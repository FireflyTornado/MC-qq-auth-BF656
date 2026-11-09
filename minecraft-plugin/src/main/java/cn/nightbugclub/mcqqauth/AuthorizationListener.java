package cn.nightbugclub.mcqqauth;

import cn.nightbugclub.mcqqauth.AuthorizationService.PlayerStatus;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerLoginEvent;
import org.bukkit.event.player.PlayerQuitEvent;

import java.util.Collection;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;
import java.util.function.Supplier;

public final class AuthorizationListener implements Listener {
    record OnlineCheck(int checked, int kicked, boolean complete) {}

    private final AuthorizationModeController modes;
    private final Supplier<AuthorizationService.View> view;
    private final Supplier<Collection<? extends Player>> onlinePlayers;
    private final String unauthorized;
    private final String notReady;
    private final String revoked;
    // The Bukkit Player instance identifies a connection. This set is main-thread only.
    private final Set<Player> exemptions = Collections.newSetFromMap(new IdentityHashMap<>());

    AuthorizationListener(AuthorizationModeController modes, Supplier<AuthorizationService.View> view,
                          Supplier<Collection<? extends Player>> onlinePlayers,
                          String unauthorized, String notReady, String revoked) {
        this.modes = modes;
        this.view = view;
        this.onlinePlayers = onlinePlayers;
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

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        exemptions.remove(event.getPlayer());
    }

    void protectCurrentSessions(AuthorizationService.View current) {
        for (Player player : onlinePlayers.get()) {
            if (current.status(player.getName()) != PlayerStatus.AUTHORIZED) {
                exemptions.add(player);
            }
        }
    }

    void clearExemptions() {
        exemptions.clear();
    }

    void checkOnlinePlayers(AuthorizationModeController.Policy policy) {
        if (policy.mode != PluginSettings.Mode.ENFORCE || view.get().snapshot() == null) {
            return;
        }
        for (Player player : List.copyOf(onlinePlayers.get())) {
            if (!modes.isCurrent(policy)) {
                return;
            }
            if (!player.isOnline()) {
                continue;
            }
            // Only a confirmed snapshot queues a scan. Disconnects alone leave that roster intact.
            AuthProtocol.Snapshot confirmed = view.get().snapshot();
            if (confirmed == null) {
                return;
            }
            if (confirmed.allows(player.getName())) {
                exemptions.remove(player);
            } else if (!exemptions.contains(player)) {
                player.kickPlayer(revoked);
            }
        }
    }

    OnlineCheck kickUnauthorizedOnline() {
        int checked = 0;
        int kicked = 0;
        if (!view.get().ready()) {
            return new OnlineCheck(checked, kicked, false);
        }
        for (Player player : List.copyOf(onlinePlayers.get())) {
            if (!player.isOnline()) {
                continue;
            }
            PlayerStatus status = view.get().status(player.getName());
            if (status == PlayerStatus.UNKNOWN || modes.mode() != PluginSettings.Mode.ENFORCE) {
                return new OnlineCheck(checked, kicked, false);
            }
            checked++;
            exemptions.remove(player);
            if (status == PlayerStatus.UNAUTHORIZED) {
                player.kickPlayer(revoked);
                if (!player.isOnline()) {
                    kicked++;
                }
            }
        }
        return new OnlineCheck(checked, kicked, true);
    }

    private String denial(String name, String unauthorizedMessage) {
        if (modes.mode() != PluginSettings.Mode.ENFORCE) {
            return null;
        }
        return switch (view.get().status(name)) {
            case AUTHORIZED -> null;
            case UNAUTHORIZED -> unauthorizedMessage;
            case UNKNOWN -> notReady;
        };
    }
}
