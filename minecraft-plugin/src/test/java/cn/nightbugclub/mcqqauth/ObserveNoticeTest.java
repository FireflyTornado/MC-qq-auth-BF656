package cn.nightbugclub.mcqqauth;

import fr.xephi.authme.events.LoginEvent;
import org.bukkit.Bukkit;
import org.bukkit.Material;
import org.bukkit.Server;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventPriority;
import org.bukkit.event.inventory.ClickType;
import org.bukkit.event.inventory.InventoryAction;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.inventory.InventoryCloseEvent;
import org.bukkit.event.inventory.InventoryDragEvent;
import org.bukkit.event.inventory.InventoryType;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.InventoryHolder;
import org.bukkit.inventory.InventoryView;
import org.bukkit.inventory.ItemFactory;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.Plugin;
import org.bukkit.plugin.EventExecutor;
import org.bukkit.plugin.PluginManager;
import org.bukkit.scheduler.BukkitScheduler;
import org.bukkit.scheduler.BukkitTask;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import org.mockito.ArgumentCaptor;

import java.util.ArrayDeque;
import java.util.List;
import java.util.Map;
import java.util.Queue;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;
import java.util.logging.Logger;

import static java.util.concurrent.TimeUnit.SECONDS;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class ObserveNoticeTest {
    private final Plugin plugin = mock(Plugin.class);
    private final Server server = mock(Server.class);
    private final BukkitScheduler scheduler = mock(BukkitScheduler.class);
    private final Logger logger = mock(Logger.class);
    private final AtomicLong clock = new AtomicLong();
    private final Queue<Scheduled> tasks = new ArrayDeque<>();
    private final ItemMeta paper = mock(ItemMeta.class);
    private final ItemMeta book = mock(ItemMeta.class);
    private final ItemMeta emerald = mock(ItemMeta.class);
    private AuthorizationService.View view = AuthorizationService.View.OFFLINE;
    private final AuthorizationModeController modes = new AuthorizationModeController(PluginSettings.Mode.OBSERVE);
    private ObserveNotice notice;
    private MockedStatic<Bukkit> bukkit;

    @BeforeEach
    void setUp() {
        when(plugin.getServer()).thenReturn(server);
        when(plugin.getLogger()).thenReturn(logger);
        when(server.getScheduler()).thenReturn(scheduler);
        when(scheduler.runTaskLater(eq(plugin), any(Runnable.class), anyLong())).thenAnswer(call ->
                enqueue(call.getArgument(1), call.getArgument(2)));
        when(scheduler.runTask(eq(plugin), any(Runnable.class))).thenAnswer(call -> enqueue(call.getArgument(1), 1));
        when(server.createInventory(any(InventoryHolder.class), eq(27), anyString())).thenAnswer(call -> {
            Inventory inventory = mock(Inventory.class);
            when(inventory.getHolder()).thenReturn(call.getArgument(0));
            when(inventory.getType()).thenReturn(InventoryType.CHEST);
            when(inventory.getSize()).thenReturn(27);
            return inventory;
        });

        ItemFactory factory = mock(ItemFactory.class);
        bukkit = mockStatic(Bukkit.class);
        bukkit.when(Bukkit::getItemFactory).thenReturn(factory);
        when(factory.getItemMeta(Material.PAPER)).thenReturn(paper);
        when(factory.getItemMeta(Material.BOOK)).thenReturn(book);
        when(factory.getItemMeta(Material.EMERALD)).thenReturn(emerald);
        when(factory.isApplicable(any(ItemMeta.class), any(Material.class))).thenReturn(true);
        when(factory.asMetaFor(any(ItemMeta.class), any(Material.class))).thenAnswer(call -> call.getArgument(0));
        when(factory.updateMaterial(any(ItemMeta.class), any(Material.class))).thenAnswer(call -> call.getArgument(1));
        start(true, 86_400);
    }

    @AfterEach
    void tearDown() {
        bukkit.close();
    }

    @Test
    void showsReadyUnboundPlayerA27SlotNoticeWithTheirRealName() {
        Client client = new Client("Steve");
        ready();
        join(client);
        verify(client.player, never()).openInventory(any(Inventory.class));
        assertEquals(30, tasks.element().delay);
        runNext();

        verify(server).createInventory(any(InventoryHolder.class), eq(27), eq("社区认证提醒"));
        verify(paper).setLore(List.of("游戏 ID：Steve", "QQ群：123456789012", "绑定指令：/绑定 Steve"));
        verify(book).setLore(argThat(lines -> lines.get(1).contains("/绑定 Steve")));
        verify(emerald).setDisplayName("我知道了，继续游戏");
        verify(client.player).openInventory(client.open.getTopInventory());
        verify(client.player, never()).kickPlayer(anyString());
        verify(logger).info("[OBSERVE] Player Steve: UNAUTHORIZED");
        verify(logger).info("[OBSERVE] Player Steve: notice shown");
    }

    @Test
    void permitsAuthorizedPlayerWithoutNoticeOrLaterRevocation() {
        Client client = new Client("StEvE");
        ready("steve");
        join(client);
        ready();
        notice.onSynchronized();
        assertNoNotice(client);
        verify(logger).info("[OBSERVE] Player StEvE: AUTHORIZED");
        verifyNoMoreInteractions(logger);
    }

    @Test
    void permitsUnknownPlayerEvenWithAStaleSnapshotAndLogsOnlyOnce() {
        Client client = new Client("Steve");
        view = new AuthorizationService.View(new AuthProtocol.Snapshot(1, Set.of()), false);
        join(client);
        notice.onSynchronized();
        notice.onSynchronized();
        assertNoNotice(client);
        verify(logger).info("[OBSERVE] Player Steve: UNKNOWN, authorization not ready");
        verifyNoMoreInteractions(logger);
    }

    @Test
    void schedulesOneNoticeWhenUnknownBecomesUnauthorized() {
        Client client = new Client("Steve");
        join(client);
        ready();
        notice.onSynchronized();
        notice.onSynchronized();
        assertEquals(1, tasks.size());
        runNext();
        verify(client.player).openInventory(any(Inventory.class));
        verify(logger).info("[OBSERVE] Player Steve: UNAUTHORIZED");
    }

    @Test
    void clearsWaitingStateWhenUnknownBecomesAuthorized() {
        Client client = new Client("Steve");
        join(client);
        ready("steve");
        notice.onSynchronized();
        ready();
        notice.onSynchronized();
        assertNoNotice(client);
        verify(logger).info("[OBSERVE] Player Steve: AUTHORIZED");
    }

    @Test
    void quitCancelsDelayedNoticeAndDiscardsUnknownVisit() {
        Client delayed = new Client("Steve");
        Client waiting = new Client("Alex");
        join(waiting);
        ready();
        join(delayed);
        quit(delayed);
        quit(waiting);
        verify(tasks.element().task).cancel();
        // Even if the scheduled action runs, it no longer owns this visit.
        runNext();
        notice.onSynchronized();
        assertNoNotice(delayed);
        assertNoNotice(waiting);
    }

    @Test
    void rechecksAuthorizationJustBeforeOpening() {
        Client client = new Client("Steve");
        ready();
        join(client);
        ready("steve");
        runNext();
        assertNoNotice(client);
        verify(logger).info("[OBSERVE] Player Steve: AUTHORIZED");
    }

    @Test
    void skipsNoticeWhenSnapshotLosesReadinessDuringDelay() {
        Client client = new Client("Steve");
        ready();
        join(client);
        view = new AuthorizationService.View(view.snapshot(), false);
        runNext();
        assertNoNotice(client);
        verify(logger).info("[OBSERVE] Player Steve: UNKNOWN, authorization not ready");
    }

    @Test
    void leavesExistingMenuOpenWithoutRetrying() {
        Client client = new Client("Steve");
        ready();
        join(client);
        client.open = inventoryView(client.player, mock(Inventory.class), InventoryType.CHEST);
        runNext();
        notice.onSynchronized();
        assertNoNotice(client);
        verify(client.player, never()).closeInventory();
    }

    @Test
    void cancelledOpenDoesNotStartCooldownOrRetryDuringTheVisit() {
        Client first = new Client("Steve");
        doReturn(null).when(first.player).openInventory(any(Inventory.class));
        ready();
        join(first);
        runNext();
        notice.onSynchronized();
        join(first);
        assertTrue(tasks.isEmpty());
        verify(logger, never()).info(contains("notice shown"));
        quit(first);

        Client again = new Client("Steve");
        join(again);
        runNext();
        verify(again.player).openInventory(any(Inventory.class));
    }

    @Test
    void retriesOnceAfterAuthMeLoginIfTheFirstOpenWasBlocked() throws Exception {
        start(true, 0);
        EventExecutor login = authMeLoginExecutor();
        Client client = new Client("Steve");
        client.allowInventoryOpen = false;
        ready();
        join(client);
        runNext();
        verify(logger, never()).info(contains("notice shown"));

        client.allowInventoryOpen = true;
        login.execute(notice, new LoginEvent(client.player));
        login.execute(notice, new LoginEvent(client.player));
        assertEquals(1, tasks.size());
        assertEquals(30, tasks.element().delay);
        runNext();
        verify(logger).info("[OBSERVE] Player Steve: notice shown");
        closeByPlayer(client);
        login.execute(notice, new LoginEvent(client.player));
        assertTrue(tasks.isEmpty());
        verify(client.player, times(2)).openInventory(any(Inventory.class));
    }

    @Test
    void authMeLoginSkipsPlayerWhoBecameAuthorizedAndWaitsForFreshSnapshotWhenUnknown() throws Exception {
        EventExecutor login = authMeLoginExecutor();
        Client bound = new Client("Steve");
        ready();
        join(bound);
        Scheduled initial = tasks.remove();
        ready("steve");
        login.execute(notice, new LoginEvent(bound.player));
        verify(initial.task).cancel();
        assertNoNotice(bound);

        Client waiting = new Client("Alex");
        join(waiting);
        initial = tasks.remove();
        view = AuthorizationService.View.OFFLINE;
        login.execute(notice, new LoginEvent(waiting.player));
        verify(initial.task).cancel();
        assertNoNotice(waiting);
        ready("steve");
        notice.onSynchronized();
        runNext();
        verify(waiting.player).openInventory(any(Inventory.class));
    }

    @Test
    void authMeLoginCannotDisplayAnOldVisitAfterModeSwitchOrQuit() throws Exception {
        EventExecutor login = authMeLoginExecutor();
        Client client = new Client("Steve");
        ready();
        join(client);
        modes.switchTo(PluginSettings.Mode.ENFORCE);
        notice.clearVisits();
        runNext();
        login.execute(notice, new LoginEvent(client.player));
        modes.switchTo(PluginSettings.Mode.OBSERVE);
        login.execute(notice, new LoginEvent(client.player));
        assertNoNotice(client);

        join(client);
        quit(client);
        runNext();
        login.execute(notice, new LoginEvent(client.player));
        assertNoNotice(client);
    }

    @Test
    void escClosesWithoutKickOrRepeatEvenWhenCooldownIsZero() {
        start(true, 0);
        Client client = show();
        closeByPlayer(client);
        notice.onSynchronized();
        join(client);
        assertTrue(tasks.isEmpty());
        verify(client.player, times(1)).openInventory(any(Inventory.class));
        verify(client.player, never()).closeInventory();
        verify(client.player, never()).kickPlayer(anyString());
    }

    @Test
    void confirmationCancelsTheClickAndClosesOnTheNextTick() {
        Client client = show();
        var click = click(client, 22, ClickType.LEFT, InventoryAction.PICKUP_ALL);
        notice.onClick(click);
        notice.onClick(click(client, 22, ClickType.RIGHT, InventoryAction.PICKUP_HALF));
        assertTrue(click.isCancelled());
        assertEquals(1, tasks.size());
        verify(client.player, never()).closeInventory();
        runNext();
        verify(client.player).closeInventory();
        verify(client.player, never()).kickPlayer(anyString());
    }

    @Test
    void deferredConfirmationDoesNotCloseAReplacementMenu() {
        Client client = show();
        notice.onClick(click(client, 22, ClickType.LEFT, InventoryAction.PICKUP_ALL));
        closeByPlayer(client);
        client.open = inventoryView(client.player, mock(Inventory.class), InventoryType.CHEST);
        verify(tasks.element().task).cancel();
        runNext();
        verify(client.player, never()).closeInventory();
    }

    @Test
    void cancelsTakingItemsShiftClickHotbarSwapsCollectingAndDragging() {
        Client client = show();
        for (InventoryClickEvent click : List.of(
                click(client, 11, ClickType.LEFT, InventoryAction.PICKUP_ALL),
                click(client, 35, ClickType.SHIFT_LEFT, InventoryAction.MOVE_TO_OTHER_INVENTORY),
                click(client, 22, ClickType.SHIFT_LEFT, InventoryAction.MOVE_TO_OTHER_INVENTORY),
                click(client, 22, ClickType.NUMBER_KEY, InventoryAction.HOTBAR_SWAP),
                click(client, 15, ClickType.SWAP_OFFHAND, InventoryAction.HOTBAR_SWAP),
                click(client, 36, ClickType.DOUBLE_CLICK, InventoryAction.COLLECT_TO_CURSOR),
                click(client, 11, ClickType.DROP, InventoryAction.DROP_ONE_SLOT))) {
            notice.onClick(click);
            assertTrue(click.isCancelled(), click.getClick().name());
        }
        InventoryDragEvent drag = drag(client);
        notice.onDrag(drag);
        assertTrue(drag.isCancelled());
        assertTrue(tasks.isEmpty());
    }

    @Test
    void leavesVanillaInventoryAndSameTitledForeignMenuEventsAlone() {
        Client client = new Client("Steve");
        for (InventoryType type : List.of(InventoryType.CRAFTING, InventoryType.CHEST)) {
            client.open = inventoryView(client.player, mock(Inventory.class), type);
            when(client.open.getTitle()).thenReturn("社区认证提醒");
            var click = click(client, 22, ClickType.LEFT, InventoryAction.PICKUP_ALL);
            var drag = drag(client);
            notice.onClick(click);
            notice.onDrag(drag);
            notice.onClose(new InventoryCloseEvent(client.open));
            assertFalse(click.isCancelled());
            assertFalse(drag.isCancelled());
        }
        assertTrue(tasks.isEmpty());
    }

    @Test
    void escStartsCaseInsensitiveCooldownAndExactExpiryAllowsDisplay() {
        Client first = show();
        closeByPlayer(first);
        quit(first);

        clock.set(SECONDS.toNanos(86_400) - 1);
        Client cooling = new Client("sTEVE");
        join(cooling);
        assertNoNotice(cooling);
        quit(cooling);

        clock.incrementAndGet();
        Client expired = new Client("StEvE");
        join(expired);
        runNext();
        verify(expired.player).openInventory(any(Inventory.class));
    }

    @Test
    void disableCancelsPendingTasksClosesOwnedGuiAndResetsCooldown() {
        Client active = show();
        notice.onClick(click(active, 22, ClickType.LEFT, InventoryAction.PICKUP_ALL));
        Client delayed = new Client("Alex");
        join(delayed);

        notice.close();
        for (Scheduled task : tasks) {
            verify(task.task).cancel();
        }
        verify(active.player).closeInventory();
        while (!tasks.isEmpty()) {
            runNext();
        }
        assertNoNotice(delayed);
        verify(active.player, times(1)).closeInventory();
        notice.onSynchronized();
        join(delayed);
        assertNoNotice(delayed);

        start(true, 86_400);
        Client afterRestart = new Client("Steve");
        join(afterRestart);
        runNext();
        verify(afterRestart.player).openInventory(any(Inventory.class));
    }

    @Test
    void modeChangesCancelOldVisitsCloseOnlyOwnedViewsAndRetainCooldown() {
        Client active = show();
        Client foreign = new Client("Other");
        join(foreign);
        runNext();
        foreign.open = inventoryView(foreign.player, mock(Inventory.class), InventoryType.CHEST);
        Client delayed = new Client("Alex");
        join(delayed);
        Scheduled scheduled = tasks.peek();

        modes.switchTo(PluginSettings.Mode.ENFORCE);
        notice.clearVisits();
        verify(scheduled.task).cancel();
        verify(active.player).closeInventory();
        verify(foreign.player, never()).closeInventory();
        Client enforceJoin = new Client("Enforced");
        join(enforceJoin);
        modes.switchTo(PluginSettings.Mode.OBSERVE);
        runNext(); // Simulate a task already selected by the scheduler before cancellation.
        notice.onSynchronized();
        assertNoNotice(delayed);
        assertNoNotice(enforceJoin);

        Client sameName = new Client("sTeVe");
        join(sameName);
        assertNoNotice(sameName);
        Client fresh = new Client("New");
        join(fresh);
        runNext();
        verify(fresh.player).openInventory(any(Inventory.class));
    }

    @Test
    void disabledNoticesStillLogInitialAndConfirmedStatus() {
        start(false, 86_400);
        Client client = new Client("Steve");
        join(client);
        ready();
        notice.onSynchronized();
        assertNoNotice(client);
        verify(logger).info("[OBSERVE] Player Steve: UNKNOWN, authorization not ready");
        verify(logger).info("[OBSERVE] Player Steve: UNAUTHORIZED");
    }

    private void start(boolean enabled, int cooldown) {
        var config = new YamlConfiguration();
        config.set("observe.notice.enabled", enabled);
        config.set("observe.notice.cooldown-seconds", cooldown);
        config.set("observe.notice.group-number", "123456789012");
        notice = new ObserveNotice(plugin, modes, () -> view, NoticeSettings.read(config), clock::get);
    }

    private EventExecutor authMeLoginExecutor() {
        PluginManager manager = mock(PluginManager.class);
        Plugin authMe = mock(Plugin.class);
        when(server.getPluginManager()).thenReturn(manager);
        when(manager.getPlugin("AuthMe")).thenReturn(authMe);
        when(authMe.isEnabled()).thenReturn(true);
        notice.listenForAuthMeLogin();
        var executor = ArgumentCaptor.forClass(EventExecutor.class);
        verify(manager).registerEvent(eq(LoginEvent.class), eq(notice), eq(EventPriority.MONITOR),
                executor.capture(), eq(plugin), eq(true));
        return executor.getValue();
    }

    private Client show() {
        Client client = new Client("Steve");
        ready();
        join(client);
        runNext();
        return client;
    }

    private void ready(String... names) {
        view = new AuthorizationService.View(new AuthProtocol.Snapshot(1, Set.of(names)), true);
    }

    private void join(Client client) {
        notice.onJoin(new PlayerJoinEvent(client.player, ""));
    }

    private void quit(Client client) {
        notice.onQuit(new PlayerQuitEvent(client.player, ""));
        when(client.player.isOnline()).thenReturn(false);
    }

    private void closeByPlayer(Client client) {
        notice.onClose(new InventoryCloseEvent(client.open));
        client.open = client.crafting;
    }

    private void assertNoNotice(Client client) {
        assertTrue(tasks.isEmpty());
        verify(client.player, never()).openInventory(any(Inventory.class));
        verify(client.player, never()).kickPlayer(anyString());
    }

    private BukkitTask enqueue(Runnable action, long delay) {
        BukkitTask task = mock(BukkitTask.class);
        tasks.add(new Scheduled(action, task, delay));
        return task;
    }

    private void runNext() {
        tasks.remove().action.run();
    }

    private static InventoryClickEvent click(Client client, int slot, ClickType click, InventoryAction action) {
        return new InventoryClickEvent(client.open, InventoryType.SlotType.CONTAINER, slot, click, action);
    }

    private static InventoryDragEvent drag(Client client) {
        var stone = new ItemStack(Material.STONE);
        return new InventoryDragEvent(client.open, null, stone, false, Map.of(0, stone));
    }

    private static InventoryView inventoryView(Player player, Inventory top, InventoryType type) {
        InventoryView view = mock(InventoryView.class);
        when(view.getPlayer()).thenReturn(player);
        when(view.getTopInventory()).thenReturn(top);
        when(view.getType()).thenReturn(type);
        return view;
    }

    private record Scheduled(Runnable action, BukkitTask task, long delay) {}

    private static final class Client {
        private final Player player = mock(Player.class);
        private final InventoryView crafting = inventoryView(player, mock(Inventory.class), InventoryType.CRAFTING);
        private InventoryView open = crafting;
        private boolean allowInventoryOpen = true;

        Client(String name) {
            when(player.getUniqueId()).thenReturn(UUID.randomUUID());
            when(player.getName()).thenReturn(name);
            when(player.isOnline()).thenReturn(true);
            when(player.getOpenInventory()).thenAnswer(call -> open);
            when(player.openInventory(any(Inventory.class))).thenAnswer(call -> {
                if (!allowInventoryOpen) {
                    return null;
                }
                open = inventoryView(player, call.getArgument(0), InventoryType.CHEST);
                return open;
            });
            doAnswer(call -> {
                open = crafting;
                return null;
            }).when(player).closeInventory();
        }
    }
}
