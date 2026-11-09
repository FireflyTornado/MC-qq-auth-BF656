package cn.nightbugclub.mcqqauth;

import cn.nightbugclub.mcqqauth.AuthorizationService.PlayerStatus;
import org.bukkit.Material;
import org.bukkit.Server;
import org.bukkit.entity.Player;
import org.bukkit.event.Event;
import org.bukkit.event.EventException;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.inventory.InventoryCloseEvent;
import org.bukkit.event.inventory.InventoryDragEvent;
import org.bukkit.event.inventory.InventoryType;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.InventoryHolder;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.Plugin;
import org.bukkit.scheduler.BukkitTask;

import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

import static java.util.concurrent.TimeUnit.SECONDS;

// All player, inventory and task state belongs to the Bukkit main thread.
final class ObserveNotice implements Listener {
    private static final int CONFIRM_SLOT = 22;

    private final Plugin plugin;
    private final AuthorizationModeController modes;
    private final Supplier<AuthorizationService.View> view;
    private final NoticeSettings settings;
    private final LongSupplier nanoTime;
    private final long cooldownNanos;
    private final Map<UUID, Visit> visits = new HashMap<>();
    private final Map<String, Long> lastShown = new HashMap<>();
    private boolean closed;

    ObserveNotice(Plugin plugin, AuthorizationModeController modes, Supplier<AuthorizationService.View> view,
                  NoticeSettings settings, LongSupplier nanoTime) {
        this.plugin = plugin;
        this.modes = modes;
        this.view = view;
        this.settings = settings;
        this.nanoTime = nanoTime;
        cooldownNanos = SECONDS.toNanos(settings.cooldownSeconds());
    }

    boolean enabled() {
        return settings.enabled();
    }

    void listenForAuthMeLogin() {
        var manager = plugin.getServer().getPluginManager();
        Plugin authMe = manager.getPlugin("AuthMe");
        if (authMe == null || !authMe.isEnabled()) {
            return;
        }
        try {
            Class<? extends Event> loginEvent = Class.forName("fr.xephi.authme.events.LoginEvent",
                    false, authMe.getClass().getClassLoader()).asSubclass(Event.class);
            Method getPlayer = loginEvent.getMethod("getPlayer");
            manager.registerEvent(loginEvent, this, EventPriority.MONITOR, (listener, event) -> {
                if (loginEvent.isInstance(event)) {
                    try {
                        onAuthMeLogin((Player) getPlayer.invoke(event));
                    } catch (ReflectiveOperationException e) {
                        throw new EventException(e);
                    }
                }
            }, plugin, true);
        } catch (ReflectiveOperationException | ClassCastException | IllegalArgumentException | LinkageError e) {
            plugin.getLogger().warning("无法监听 AuthMe 登录成功事件，观察公告仅在入服时安排。");
        }
    }

    private boolean observing() {
        return !closed && modes.mode() == PluginSettings.Mode.OBSERVE;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        if (!observing()) {
            return;
        }
        Player player = event.getPlayer();
        UUID id = player.getUniqueId();
        Visit previous = visits.get(id);
        if (previous != null) {
            if (previous.player == player) {
                return;
            }
            previous.cancelTask();
        }
        Visit visit = new Visit(player);
        visits.put(id, visit);
        evaluate(visit, view.get().status(player.getName()));
    }

    private void onAuthMeLogin(Player player) {
        if (!observing()) {
            return;
        }
        Visit visit = visits.get(player.getUniqueId());
        if (visit == null || visit.player != player || visit.noticeShown || visit.authMeLoginHandled) {
            return;
        }
        visit.authMeLoginHandled = true;
        visit.cancelTask();
        evaluate(visit, view.get().status(player.getName()));
    }

    void onSynchronized() {
        if (!observing()) {
            return;
        }
        AuthorizationService.View current = view.get();
        if (!current.ready()) {
            return;
        }
        for (Visit visit : visits.values()) {
            if (visit.awaitingSnapshot && visit.player.isOnline()) {
                evaluate(visit, current.status(visit.player.getName()));
            }
        }
    }

    private void evaluate(Visit visit, PlayerStatus status) {
        visit.awaitingSnapshot = status == PlayerStatus.UNKNOWN;
        logStatus(visit.player, status);
        if (status == PlayerStatus.UNAUTHORIZED && enabled() && !onCooldown(visit)) {
            visit.task = plugin.getServer().getScheduler()
                    .runTaskLater(plugin, () -> show(visit), settings.delayTicks());
        }
    }

    private boolean onCooldown(Visit visit) {
        long now = nanoTime.getAsLong();
        lastShown.values().removeIf(shown -> now - shown >= cooldownNanos);
        return lastShown.containsKey(visit.nameKey);
    }

    private void show(Visit visit) {
        visit.task = null;
        Player player = visit.player;
        if (!observing() || visits.get(player.getUniqueId()) != visit || visit.noticeShown || !player.isOnline()
                || player.getOpenInventory().getType() != InventoryType.CRAFTING || onCooldown(visit)) {
            return;
        }

        NoticeInventory candidate = new NoticeInventory(plugin.getServer(), settings, player.getName());
        PlayerStatus current = view.get().status(player.getName());
        if (current != PlayerStatus.UNAUTHORIZED) {
            logStatus(player, current);
            return;
        }

        visit.notice = candidate;
        player.openInventory(candidate.getInventory());
        if (!observing() || visits.get(player.getUniqueId()) != visit) {
            // Another plugin can run a mode command from InventoryOpenEvent.
            closeInventory(player, candidate);
        } else if (isOpen(player, candidate)) {
            visit.noticeShown = true;
            lastShown.put(visit.nameKey, nanoTime.getAsLong());
            plugin.getLogger().info("[OBSERVE] Player " + player.getName() + ": notice shown");
        } else {
            // InventoryOpenEvent may have been cancelled by another plugin.
            visit.notice = null;
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onClick(InventoryClickEvent event) {
        if (!(event.getView().getTopInventory().getHolder() instanceof NoticeInventory notice)) {
            return;
        }
        boolean canConfirm = !event.isCancelled() && !event.isShiftClick()
                && (event.isLeftClick() || event.isRightClick()) && event.getRawSlot() == CONFIRM_SLOT;
        event.setCancelled(true);
        Visit visit = visits.get(event.getWhoClicked().getUniqueId());
        if (canConfirm && visit != null && visit.notice == notice && !visit.acknowledged) {
            visit.acknowledged = true;
            // Bukkit forbids closing the view inside InventoryClickEvent.
            visit.task = plugin.getServer().getScheduler().runTask(plugin, () -> {
                visit.task = null;
                closeInventory(visit.player, notice);
                visit.notice = null;
            });
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onDrag(InventoryDragEvent event) {
        if (event.getView().getTopInventory().getHolder() instanceof NoticeInventory) {
            event.setCancelled(true);
        }
    }

    @EventHandler
    public void onClose(InventoryCloseEvent event) {
        Visit visit = visits.get(event.getPlayer().getUniqueId());
        if (visit != null && visit.notice != null && event.getView().getTopInventory().getHolder() == visit.notice) {
            visit.cancelTask();
            visit.notice = null;
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        Player player = event.getPlayer();
        Visit visit = visits.get(player.getUniqueId());
        if (visit != null && visit.player == player) {
            visits.remove(player.getUniqueId());
            visit.cancelTask();
        }
    }

    void close() {
        closed = true;
        clearVisits();
        lastShown.clear();
    }

    void clearVisits() {
        List<Visit> remaining = List.copyOf(visits.values());
        visits.clear();
        for (Visit visit : remaining) {
            visit.cancelTask();
            closeInventory(visit.player, visit.notice);
            visit.notice = null;
        }
    }

    private void logStatus(Player player, PlayerStatus status) {
        plugin.getLogger().info("[OBSERVE] Player " + player.getName() + ": " + status
                + (status == PlayerStatus.UNKNOWN ? ", authorization not ready" : ""));
    }

    private static boolean isOpen(Player player, NoticeInventory notice) {
        return notice != null && player.isOnline()
                && player.getOpenInventory().getTopInventory().getHolder() == notice;
    }

    private static void closeInventory(Player player, NoticeInventory notice) {
        if (isOpen(player, notice)) {
            player.closeInventory();
        }
    }

    private static final class Visit {
        private final Player player;
        private final String nameKey;
        private boolean awaitingSnapshot;
        private boolean authMeLoginHandled;
        private boolean noticeShown;
        private boolean acknowledged;
        private BukkitTask task;
        private NoticeInventory notice;

        Visit(Player player) {
            this.player = player;
            nameKey = player.getName().toLowerCase(Locale.ROOT);
        }

        void cancelTask() {
            if (task != null) {
                task.cancel();
                task = null;
            }
        }
    }

    private static final class NoticeInventory implements InventoryHolder {
        private final Inventory inventory;

        NoticeInventory(Server server, NoticeSettings settings, String player) {
            inventory = server.createInventory(this, 27, settings.titleFor(player));
            inventory.setItem(11, item(Material.BOOK, "社区认证说明", settings.messageFor(player)));
            inventory.setItem(15, item(Material.PAPER, "QQ群与绑定步骤",
                    List.of("游戏 ID：" + player, "QQ群：" + settings.groupLabel(), "绑定指令：/绑定 " + player)));
            inventory.setItem(CONFIRM_SLOT, item(Material.EMERALD, settings.buttonFor(player), List.of()));
        }

        @Override
        public Inventory getInventory() {
            return inventory;
        }

        private static ItemStack item(Material material, String name, List<String> lore) {
            ItemStack item = new ItemStack(material);
            ItemMeta meta = item.getItemMeta();
            meta.setDisplayName(name);
            meta.setLore(lore);
            item.setItemMeta(meta);
            return item;
        }
    }
}
