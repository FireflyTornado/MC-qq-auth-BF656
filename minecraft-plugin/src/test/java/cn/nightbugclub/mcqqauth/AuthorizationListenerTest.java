package cn.nightbugclub.mcqqauth;

import org.bukkit.entity.Player;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerLoginEvent;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AuthorizationListenerTest {
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
            listener.scheduleOnlineCheck();
            listener.scheduleOnlineCheck();
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
        listener.scheduleOnlineCheck();
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
        listener.scheduleOnlineCheck();
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
        AuthorizationListener listener = new AuthorizationListener(view::get, () -> online, mainTasks::add,
                "未授权", "未就绪", "已撤销");
        setSnapshot(2, true);
        listener.scheduleOnlineCheck();
        mainTasks.remove().run();
        assertTrue(online.isEmpty());
    }

    private AuthorizationListener listener(Player... online) {
        return new AuthorizationListener(view::get, () -> List.of(online), mainTasks::add,
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
        return player;
    }
}
