package cn.nightbugclub.mcqqauth;

import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerLoginEvent;

import java.util.Collection;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import java.util.function.Supplier;

public final class AuthorizationListener implements Listener {
    private final Supplier<AuthorizationService.View> view;
    private final Supplier<Collection<? extends Player>> onlinePlayers;
    private final Consumer<Runnable> runOnMainThread;
    private final String unauthorized;
    private final String notReady;
    private final String revoked;
    private final AtomicBoolean scanQueued = new AtomicBoolean();

    AuthorizationListener(Supplier<AuthorizationService.View> view,
                          Supplier<Collection<? extends Player>> onlinePlayers,
                          Consumer<Runnable> runOnMainThread,
                          String unauthorized, String notReady, String revoked) {
        this.view = view;
        this.onlinePlayers = onlinePlayers;
        this.runOnMainThread = runOnMainThread;
        this.unauthorized = unauthorized;
        this.notReady = notReady;
        this.revoked = revoked;
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onPreLogin(AsyncPlayerPreLoginEvent event) {
        if (event.getLoginResult() == AsyncPlayerPreLoginEvent.Result.ALLOWED) {
            String reason = denial(event.getName(), unauthorized);
            if (reason != null) {
                event.disallow(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, reason);
            }
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onLogin(PlayerLoginEvent event) {
        if (event.getResult() == PlayerLoginEvent.Result.ALLOWED) {
            String reason = denial(event.getPlayer().getName(), unauthorized);
            if (reason != null) {
                event.disallow(PlayerLoginEvent.Result.KICK_OTHER, reason);
            }
        }
    }

    @EventHandler(priority = EventPriority.LOWEST)
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        String reason = denial(player.getName(), revoked);
        if (reason != null) {
            player.kickPlayer(reason);
        }
    }

    void scheduleOnlineCheck() {
        if (scanQueued.compareAndSet(false, true)) {
            runOnMainThread.accept(this::checkOnlinePlayers);
        }
    }

    private void checkOnlinePlayers() {
        scanQueued.set(false);
        if (view.get().snapshot() == null) {
            return;
        }
        for (Player player : List.copyOf(onlinePlayers.get())) {
            // Only a confirmed snapshot queues a scan. Disconnects alone leave that roster intact.
            AuthProtocol.Snapshot confirmed = view.get().snapshot();
            if (confirmed != null && !confirmed.allows(player.getName())) {
                player.kickPlayer(revoked);
            }
        }
    }

    private String denial(String name, String unauthorizedMessage) {
        AuthorizationService.View current = view.get();
        if (!current.ready()) {
            return notReady;
        }
        return current.snapshot().allows(name) ? null : unauthorizedMessage;
    }
}
