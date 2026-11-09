package cn.nightbugclub.mcqqauth;

import org.bukkit.entity.Player;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerLoginEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import static cn.nightbugclub.mcqqauth.PluginSettings.Mode.*;

class AuthorizationListenerTest {
    private final AuthorizationModeController modes = new AuthorizationModeController(ENFORCE);
    private final AtomicReference<AuthorizationService.View> view =
            new AtomicReference<>(AuthorizationService.View.OFFLINE);
    private final BlockingQueue<Runnable> mainTasks = new LinkedBlockingQueue<>();

    @Test
    void preLoginChecksReadinessAndNormalizedNameAndKeepsPriorDenial() {
        AuthorizationListener listener = listener();
        AsyncPlayerPreLoginEvent waiting = preLogin("Steve");
        listener.onPreLogin(waiting);
        assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, waiting.getLoginResult());
        assertEquals("未就绪", waiting.getKickMessage());

        setSnapshot(1, true, "steve");
        AsyncPlayerPreLoginEvent accepted = preLogin("StEvE");
        listener.onPreLogin(accepted);
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, accepted.getLoginResult());
        for (String name : List.of("Alex", "st", " Steve", "Ｓteve")) {
            AsyncPlayerPreLoginEvent denied = preLogin(name);
            listener.onPreLogin(denied);
            assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, denied.getLoginResult());
            assertEquals("未授权", denied.getKickMessage());
        }

        AsyncPlayerPreLoginEvent prior = preLogin("Alex");
        prior.disallow(AsyncPlayerPreLoginEvent.Result.KICK_BANNED, "既有拒绝");
        listener.onPreLogin(prior);
        assertEquals(AsyncPlayerPreLoginEvent.Result.KICK_BANNED, prior.getLoginResult());
        assertEquals("既有拒绝", prior.getKickMessage());
    }

    @Test
    void rechecksOnMainThreadIfStateChangesDuringLogin() {
        Player steve = player("Steve");
        AuthorizationListener listener = listener(steve);
        setSnapshot(1, true, "steve");
        AsyncPlayerPreLoginEvent accepted = preLogin("Steve");
        listener.onPreLogin(accepted);
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, accepted.getLoginResult());
        setSnapshot(2, true);
        var login = new PlayerLoginEvent(steve, "localhost", InetAddress.getLoopbackAddress());
        listener.onLogin(login);
        assertEquals(PlayerLoginEvent.Result.KICK_OTHER, login.getResult());
        assertEquals("未授权", login.getKickMessage());
        login.disallow(PlayerLoginEvent.Result.KICK_WHITELIST, "既有拒绝");
        listener.onLogin(login);
        assertEquals("既有拒绝", login.getKickMessage());

        listener.onJoin(new PlayerJoinEvent(steve, ""));
        verify(steve).kickPlayer("已撤销");
    }

    @Test
    void schedulesCoalescedOnlineCheckAndUsesLatestConfirmedState() throws Exception {
        Player steve = player("Steve");
        Player alex = player("Alex");
        AuthorizationListener listener = listener(steve, alex);
        setSnapshot(2, true, "steve", "alex");
        Thread sync = new Thread(() -> {
            queueScan(listener);
            queueScan(listener);
        });
        sync.start();
        sync.join();
        assertEquals(1, mainTasks.size());
        verify(steve, never()).kickPlayer(anyString());
        setSnapshot(2, false, "steve", "alex");
        mainTasks.remove().run();
        verify(steve, never()).kickPlayer(anyString());
        verify(alex, never()).kickPlayer(anyString());

        setSnapshot(2, true, "alex");
        queueScan(listener);
        setSnapshot(3, true, "steve");
        mainTasks.remove().run();
        verify(steve, never()).kickPlayer(anyString());
        verify(alex).kickPlayer("已撤销");
    }

    @Test
    void disconnectDoesNotUndoAnAlreadyConfirmedRevocation() {
        Player steve = player("Steve");
        Player alex = player("Alex");
        AuthorizationListener listener = listener(steve, alex);
        setSnapshot(2, true, "alex");
        queueScan(listener);
        setSnapshot(2, false, "alex");
        mainTasks.remove().run();
        verify(steve).kickPlayer("已撤销");
        verify(alex, never()).kickPlayer(anyString());
    }

    @Test
    void kickingMultiplePlayersToleratesChangesToBukkitOnlineView() {
        Player steve = player("Steve");
        Player alex = player("Alex");
        List<Player> online = new ArrayList<>(List.of(steve, alex));
        doAnswer(call -> online.remove(steve)).when(steve).kickPlayer("已撤销");
        doAnswer(call -> online.remove(alex)).when(alex).kickPlayer("已撤销");
        AuthorizationListener listener = new AuthorizationListener(modes, view::get, () -> online,
                "未授权", "未就绪", "已撤销");
        setSnapshot(2, true);
        queueScan(listener);
        mainTasks.remove().run();
        assertTrue(online.isEmpty());
    }

    @Test
    void transitionProtectsUnknownAndUnauthorizedConnectionsUntilQuitOrAuthorization() {
        Player protectedPlayer = player("A");
        Player previouslyAuthorized = player("Bb");
        Player laterAuthorized = player("Cc");
        Player reconnected = player("A");
        UUID id = UUID.randomUUID();
        when(protectedPlayer.getUniqueId()).thenReturn(id);
        when(reconnected.getUniqueId()).thenReturn(id);
        List<Player> online = new ArrayList<>(List.of(protectedPlayer, previouslyAuthorized, laterAuthorized));
        AuthorizationListener listener = new AuthorizationListener(modes, view::get, () -> online,
                "未授权", "未就绪", "已撤销");
        modes.switchTo(OBSERVE);
        setSnapshot(1, true, "bb");
        listener.protectCurrentSessions(view.get());
        modes.switchTo(ENFORCE);

        AsyncPlayerPreLoginEvent newcomer = preLogin("A");
        listener.onPreLogin(newcomer);
        assertEquals("未授权", newcomer.getKickMessage());
        online.forEach(player -> verify(player, never()).kickPlayer(anyString()));
        setSnapshot(2, true, "cc");
        queueScan(listener);
        mainTasks.remove().run();
        verify(previouslyAuthorized).kickPlayer("已撤销");
        online.remove(previouslyAuthorized);
        verify(protectedPlayer, never()).kickPlayer(anyString());
        verify(laterAuthorized, never()).kickPlayer(anyString());

        setSnapshot(3, true);
        queueScan(listener);
        mainTasks.remove().run();
        verify(laterAuthorized).kickPlayer("已撤销");
        online.remove(laterAuthorized);
        verify(protectedPlayer, never()).kickPlayer(anyString());

        listener.onQuit(new PlayerQuitEvent(protectedPlayer, ""));
        online.remove(protectedPlayer);
        online.add(reconnected);
        PlayerLoginEvent retry = new PlayerLoginEvent(reconnected, "localhost", InetAddress.getLoopbackAddress());
        listener.onLogin(retry);
        assertEquals(PlayerLoginEvent.Result.KICK_OTHER, retry.getResult());
        queueScan(listener);
        mainTasks.remove().run();
        verify(reconnected).kickPlayer("已撤销");
    }

    @Test
    void protectsUnknownSessionAndRejectsNewLoginsUntilServiceIsReady() {
        Player online = player("Steve");
        AuthorizationListener listener = listener(online);
        modes.switchTo(OBSERVE);
        listener.protectCurrentSessions(view.get());
        modes.switchTo(ENFORCE);
        AsyncPlayerPreLoginEvent login = preLogin("Steve");
        listener.onPreLogin(login);
        assertEquals("未就绪", login.getKickMessage());
        setSnapshot(1, true);
        queueScan(listener);
        mainTasks.remove().run();
        verify(online, never()).kickPlayer(anyString());
    }

    @Test
    void queuedScansCannotCrossModeChangesOrDisable() {
        Player online = player("Steve");
        AuthorizationListener listener = listener(online);
        setSnapshot(1, true);
        queueScan(listener);
        modes.switchTo(OBSERVE);
        AsyncPlayerPreLoginEvent login = preLogin("Steve");
        listener.onPreLogin(login);
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, login.getLoginResult());
        listener.onJoin(new PlayerJoinEvent(online, ""));
        modes.switchTo(ENFORCE);
        mainTasks.remove().run();
        verify(online, never()).kickPlayer(anyString());
        queueScan(listener);
        modes.close();
        mainTasks.remove().run();
        verify(online, never()).kickPlayer(anyString());
    }

    @Test
    void loginAlreadyBeingCheckedCanFinishUnderThePolicyItRead() throws Exception {
        CountDownLatch checking = new CountDownLatch(1);
        CountDownLatch proceed = new CountDownLatch(1);
        AuthorizationListener listener = new AuthorizationListener(modes, () -> {
            checking.countDown();
            try {
                assertTrue(proceed.await(5, java.util.concurrent.TimeUnit.SECONDS));
            } catch (InterruptedException e) {
                throw new AssertionError(e);
            }
            return view.get();
        }, List::of, "未授权", "未就绪", "已撤销");
        AsyncPlayerPreLoginEvent first = preLogin("Steve");
        var pending = new java.util.concurrent.FutureTask<Void>(() -> {
            listener.onPreLogin(first);
            return null;
        });
        new Thread(pending).start();
        try {
            assertTrue(checking.await(5, java.util.concurrent.TimeUnit.SECONDS));
            modes.switchTo(OBSERVE);
        } finally {
            proceed.countDown();
        }
        pending.get(5, java.util.concurrent.TimeUnit.SECONDS);
        assertEquals("未就绪", first.getKickMessage());
        AsyncPlayerPreLoginEvent next = preLogin("Steve");
        listener.onPreLogin(next);
        assertEquals(AsyncPlayerPreLoginEvent.Result.ALLOWED, next.getLoginResult());
    }

    @Test
    void joinRechecksWhenEnforceBeginsAfterLoginWasAllowed() {
        Player player = player("Steve");
        AuthorizationListener listener = listener();
        modes.switchTo(OBSERVE);
        PlayerLoginEvent login = new PlayerLoginEvent(player, "localhost", InetAddress.getLoopbackAddress());
        listener.onLogin(login);
        assertEquals(PlayerLoginEvent.Result.ALLOWED, login.getResult());
        setSnapshot(1, true);
        modes.switchTo(ENFORCE);
        listener.onJoin(new PlayerJoinEvent(player, ""));
        verify(player).kickPlayer("已撤销");
    }

    @Test
    void explicitSweepStopsBeforeUsingAStaleRosterAndDoesNotCountACancelledKick() {
        Player cancelled = player("One");
        Player kicked = player("Two");
        Player remaining = player("Three");
        AuthorizationListener listener = listener(cancelled, kicked, remaining);
        setSnapshot(1, true);
        doAnswer(call -> {
            when(kicked.isOnline()).thenReturn(false);
            setSnapshot(1, false);
            return null;
        }).when(kicked).kickPlayer("已撤销");

        assertEquals(new AuthorizationListener.OnlineCheck(2, 1, false), listener.kickUnauthorizedOnline());
        verify(cancelled).kickPlayer("已撤销");
        verify(remaining, never()).kickPlayer(anyString());
    }

    private void queueScan(AuthorizationListener listener) {
        modes.scheduleSync(mainTasks::add, listener::checkOnlinePlayers);
    }

    private AuthorizationListener listener(Player... online) {
        return new AuthorizationListener(modes, view::get, () -> List.of(online),
                "未授权", "未就绪", "已撤销");
    }

    private void setSnapshot(long version, boolean ready, String... names) {
        view.set(new AuthorizationService.View(new AuthProtocol.Snapshot(version, Set.of(names)), ready));
    }

    private static AsyncPlayerPreLoginEvent preLogin(String name) {
        return new AsyncPlayerPreLoginEvent(name, InetAddress.getLoopbackAddress(), UUID.randomUUID());
    }

    private static Player player(String name) {
        Player player = mock(Player.class);
        when(player.getName()).thenReturn(name);
        when(player.isOnline()).thenReturn(true);
        return player;
    }
}
