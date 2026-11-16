package cn.nightbugclub.mcqqauth;

import org.apache.logging.log4j.LogManager;
import org.bukkit.Bukkit;
import org.bukkit.Server;
import org.bukkit.command.Command;
import org.bukkit.command.CommandException;
import org.bukkit.command.CommandSender;
import org.bukkit.command.FormattedCommandAlias;
import org.bukkit.command.RemoteConsoleCommandSender;
import org.bukkit.command.SimpleCommandMap;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.plugin.PluginManager;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.net.SocketAddress;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class SudoCommandsTest {
    @TempDir
    Path directory;

    private final Server server = mock(Server.class);
    private final RemoteConsoleCommandSender console = mock(RemoteConsoleCommandSender.class);

    @ParameterizedTest
    @ValueSource(strings = {"op Steve", "stop", "minecraft:whitelist add Steve", "whitelist remove Steve",
            "whitelist addall Steve", "whitelist-extra add Steve", "whitelist add Steve\nsay Hi",
            "whitelist add Steve\u2028stop"})
    void rejectsCommandsOutsideTheTokenPrefix(String input) {
        assertEquals("rejected", new SudoCommands(server, Set.of("whitelist add")).execute(input).status());
        verifyNoInteractions(server);
    }

    @Test
    void emptyFileAllowsNothingAndInvalidRulesFailClosed() throws Exception {
        Path file = directory.resolve(SudoCommands.ALLOWLIST_FILE);
        Files.writeString(file, "# empty\n\n");
        var commands = new SudoCommands(server, SudoCommands.readAllowlist(file));
        assertEquals("rejected", commands.execute("list").status());
        verifyNoInteractions(server);
        Files.writeString(file, "whitelist\n/whitelist add\n");
        var error = assertThrows(IllegalArgumentException.class, () -> SudoCommands.readAllowlist(file));
        assertTrue(error.getMessage().contains("第 2 行"));
    }

    @Test
    void configuredPrefixesDispatchWholeCommandsWithOriginalArguments() throws Exception {
        Path file = directory.resolve(SudoCommands.ALLOWLIST_FILE);
        Files.writeString(file, "\uFEFF# UTF-8\r\n whitelist add \r\nwhitelist add\r\nexample:inspect world\r\n");
        var prefixes = SudoCommands.readAllowlist(file);
        assertEquals(Set.of("whitelist add", "example:inspect world"), prefixes);
        var commands = new SudoCommands(server, prefixes,
                () -> new MohistCommandFeedback.Capture(console, new BoundedCommandOutput()));
        when(server.dispatchCommand(console, "whitelist add Steve Alex")).thenReturn(true);
        when(server.dispatchCommand(console, "example:inspect world \"夜间 测试\"")).thenReturn(true);
        assertEquals("ok", commands.execute("whitelist add Steve Alex").status());
        assertEquals("ok", commands.execute("example:inspect world \"夜间 测试\"").status());
        verify(server).dispatchCommand(console, "whitelist add Steve Alex");
        verify(server).dispatchCommand(console, "example:inspect world \"夜间 测试\"");
    }

    @Test
    void rootPrefixIncludesExactCommandAndEverySubcommand() {
        var commands = new SudoCommands(server, Set.of("whitelist"),
                () -> new MohistCommandFeedback.Capture(console, new BoundedCommandOutput()));
        when(server.dispatchCommand(eq(console), anyString())).thenReturn(true);
        for (String input : Set.of("whitelist", "whitelist on", "whitelist remove Steve", "whitelist add Alex")) {
            assertEquals("ok", commands.execute(input).status());
            verify(server).dispatchCommand(console, input);
        }
    }

    @Test
    void shortNameUsesServerBindingAndQualifiedNameSelectsTheOtherCommand() {
        var commandMap = new SimpleCommandMap(server);
        var plugin = greetingCommand("plugin command");
        var vanilla = greetingCommand("vanilla command");
        commandMap.register("greet", "example", plugin);
        commandMap.register("greet", "minecraft", vanilla);
        assertSame(plugin, commandMap.getCommand("greet"));
        assertSame(vanilla, commandMap.getCommand("minecraft:greet"));
        when(server.dispatchCommand(eq(console), anyString()))
                .thenAnswer(call -> commandMap.dispatch(console, call.getArgument(1, String.class)));

        var commands = new SudoCommands(server, Set.of("greet", "minecraft:greet"),
                () -> new MohistCommandFeedback.Capture(console, new BoundedCommandOutput()));
        try (var bukkit = mockStatic(Bukkit.class)) {
            bukkit.when(Bukkit::getPluginManager).thenReturn(mock(PluginManager.class));
            assertEquals("ok", commands.execute("greet").status());
            verify(console).sendMessage("plugin command");
            verify(console, never()).sendMessage("vanilla command");
            assertEquals("ok", commands.execute("minecraft:greet").status());
            verify(console).sendMessage("vanilla command");
        }
    }

    private static Command greetingCommand(String message) {
        return new Command("greet") {
            @Override
            public boolean execute(CommandSender sender, String label, String[] args) {
                sender.sendMessage(message);
                return true;
            }
        };
    }

    @ParameterizedTest
    @ValueSource(strings = {"greet", "example:greet"})
    void rejectsConfiguredAliasesBeforeDispatch(String alias) {
        var commandMap = new SimpleCommandMap(server);
        commandMap.register("first", "example", mock(Command.class));
        commandMap.register("second", "example", mock(Command.class));
        when(server.getCommandAliases()).thenReturn(
                Map.of(alias, new String[]{"example:first", "example:second"}));
        commandMap.registerServerAliases();
        assertInstanceOf(FormattedCommandAlias.class, commandMap.getCommand(alias));

        var commands = new SudoCommands(server, Set.of(alias),
                () -> new MohistCommandFeedback.Capture(console, new BoundedCommandOutput()));
        assertEquals("rejected", commands.execute(alias + " argument").status());
        verify(server, never()).dispatchCommand(any(), anyString());
    }

    @Test
    void returnsNativeFeedbackEvenOnDispatchFailureAndIsolatesEachRequest() {
        var commands = new SudoCommands(server, Set.of("list"), () -> {
            var sender = mock(RemoteConsoleCommandSender.class);
            var output = new BoundedCommandOutput();
            doAnswer(call -> {
                output.appendSender(call.getArgument(0, String.class));
                return null;
            }).when(sender).sendMessage(anyString());
            return new MohistCommandFeedback.Capture(sender, output);
        });
        when(server.dispatchCommand(any(RemoteConsoleCommandSender.class), eq("list")))
                .thenAnswer(call -> {
                    call.getArgument(0, RemoteConsoleCommandSender.class).sendMessage("Players:\nAlex, Steve\n");
                    return true;
                })
                .thenAnswer(call -> {
                    call.getArgument(0, RemoteConsoleCommandSender.class).sendMessage("Incorrect argument\n");
                    throw new CommandException("test failure");
                })
                .thenReturn(false);
        assertEquals(new SudoCommands.Result("ok", "Players:\nAlex, Steve\n"), commands.execute("list"));
        assertEquals(new SudoCommands.Result("error", "Incorrect argument\n"), commands.execute("list"));
        assertEquals(new SudoCommands.Result("error", "服务端未找到该命令。"), commands.execute("list"));
        assertFalse(commands.isDispatching());
    }

    @Test
    void fallsBackToSameThreadConsoleLogsAndDetachesAfterDispatch() {
        var logger = LogManager.getLogger(SudoCommandsTest.class);
        var commands = new SudoCommands(server, Set.of("list"), () -> {
            var output = new BoundedCommandOutput();
            doAnswer(call -> {
                output.appendSender(call.getArgument(0, String.class));
                return null;
            }).when(console).sendMessage(anyString());
            return new MohistCommandFeedback.Capture(console, output);
        });
        when(server.dispatchCommand(console, "list")).thenAnswer(call -> {
            logger.error("first command log");
            CompletableFuture.runAsync(() -> logger.error("unrelated worker log")).join();
            console.sendMessage("sender reply\n");
            logger.error("second command log");
            throw new CommandException("expected test failure");
        }).thenReturn(true);

        assertEquals(new SudoCommands.Result("error", "first command log\nsender reply\nsecond command log\n"),
                commands.execute("list"));
        logger.error("outside dispatch");
        assertEquals(new SudoCommands.Result("ok", "命令已返回，未捕获到输出。扩展采集模块未启用。"),
                commands.execute("list"));
    }

    @Test
    void unavailableFeedbackDoesNotDispatch() {
        var commands = new SudoCommands(server, Set.of("list"), () -> {
            throw new IllegalStateException("No compatible feedback source");
        });
        assertEquals(new SudoCommands.Result("error", "服务端命令反馈接口不可用，指令未执行。"),
                commands.execute("list"));
        verify(server, never()).dispatchCommand(any(), anyString());
    }

    @Test
    void waitsForBothSenderFeedbackAndTrackedCallbacks() throws Exception {
        var completed = new CompletableFuture<Boolean>();
        var output = new BoundedCommandOutput();
        var commands = new SudoCommands(server, Set.of("say"), () ->
                new MohistCommandFeedback.Capture(console, output, () -> () -> completed));
        when(server.dispatchCommand(console, "say hello")).thenReturn(true);

        var waiting = commands.execute("say hello", System.currentTimeMillis() + 30_000);
        assertFalse(waiting.isDone());
        assertFalse(commands.isDispatching());
        completed.complete(true);
        assertFalse(waiting.isDone());
        output.appendSender("late sender feedback\n");
        assertEquals(new SudoCommands.Result("ok", "late sender feedback\n"), waiting.get(2, TimeUnit.SECONDS));
    }

    @Test
    void keepsTheCommandStatusAndCollectedOutputWhenTrackingTimesOut() {
        var output = new BoundedCommandOutput();
        output.appendLine("hello");
        var incomplete = new SudoCommands(server, Set.of("say"), () ->
                new MohistCommandFeedback.Capture(console, output, () -> CompletableFuture::new));
        when(server.dispatchCommand(console, "say hello")).thenReturn(true);
        assertEquals(new SudoCommands.Result("ok", "hello\n", false, true), incomplete.execute("say hello"));
    }

    @Test
    void measuresQuietFromSenderTextOnlyAndClosesTheSharedBuffer() {
        var time = new AtomicLong();
        var output = new BoundedCommandOutput(time::get);
        output.appendLine("player packet");
        output.startSenderWait(3000);
        assertEquals(TimeUnit.MILLISECONDS.toNanos(3000), output.senderWaitNanos());
        time.set(TimeUnit.MILLISECONDS.toNanos(800));
        output.appendSender("sender reply\n");
        assertEquals(TimeUnit.MILLISECONDS.toNanos(500), output.senderWaitNanos());
        time.set(TimeUnit.MILLISECONDS.toNanos(1300));
        output.appendLine("tracked log");
        assertEquals(0, output.senderWaitNanos());
        var finished = output.finish();
        output.appendSender("too late");
        assertEquals("player packet\nsender reply\ntracked log\n", finished.text());
        assertEquals(finished, output.snapshot());
    }

    @Test
    void retainsFullUtf8BudgetAndTruncatesOnCharacterBoundaryWithNotice() {
        var output = new BoundedCommandOutput();
        String limit = "😀".repeat(16_384);
        assertFalse(output.append(limit));
        assertEquals(new BoundedCommandOutput.Snapshot(limit, false), output.snapshot());

        assertTrue(output.append(" extra"));
        String notice = "\n[输出超过 64 KiB，已截断。]\n";
        int prefixLength = (65_536 - notice.getBytes(StandardCharsets.UTF_8).length) / 4;
        var expected = new BoundedCommandOutput.Snapshot("😀".repeat(prefixLength) + notice, true);
        assertEquals(expected, output.snapshot());
        assertTrue(output.append(limit));
        assertEquals(expected, output.snapshot());
    }

    @Test
    void generatedSourceRoutesBothFeedbackMethodsBeforeTheOriginalBuffer() throws Exception {
        var output = new BoundedCommandOutput();
        var constructor = MohistCommandFeedback.createSource(FeedbackSource.class, Object.class,
                FeedbackSource.class.getMethod("m_213846_", FeedbackComponent.class),
                FeedbackSource.class.getMethod("sendMessage", String.class));
        Consumer<FeedbackComponent> appendComponent = component -> output.append(component.text());
        Consumer<String> appendString = output::append;
        var source = (FeedbackSource) constructor.newInstance(new Object(), null, appendComponent, appendString);
        source.m_213846_(new FeedbackComponent("原版反馈\n"));
        source.sendMessage("插件反馈\n");
        assertEquals(new BoundedCommandOutput.Snapshot("原版反馈\n插件反馈\n", false), output.snapshot());
        source.m_213846_(new FeedbackComponent("a".repeat(100_000)));
        var snapshot = output.snapshot();
        assertTrue(snapshot.truncated());
        assertTrue(snapshot.text().startsWith("原版反馈\n插件反馈\n"));
        assertTrue(snapshot.text().getBytes(StandardCharsets.UTF_8).length <= 65_536);
        source.sendMessage("ignored");
        assertEquals(snapshot, output.snapshot());
    }

    public record FeedbackComponent(String text) {}

    public static class FeedbackSource {
        public FeedbackSource(Object server, SocketAddress address) {}

        public void m_213846_(FeedbackComponent component) {
            fail("Vanilla output must bypass the original buffer");
        }

        public void sendMessage(String text) {
            fail("Plugin output must bypass the original buffer");
        }
    }

    @Test
    void enabledConfigurationRequiresSecureChannel() {
        var config = new YamlConfiguration();
        config.set("sudo.enabled", false);
        assertNull(SudoChannel.Settings.read(config, "auth-token"));
        config.set("sudo.enabled", true);
        config.set("sudo.token", "distinct-token");
        config.set("sudo.websocket-url", "ws://example.net/internal/mc/sudo/ws");
        assertThrows(IllegalArgumentException.class, () -> SudoChannel.Settings.read(config, "auth-token"));
        config.set("sudo.websocket-url", "wss://example.net/internal/mc/sudo/ws");
        assertNotNull(SudoChannel.Settings.read(config, "auth-token"));
    }
}
