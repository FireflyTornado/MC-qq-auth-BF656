package cn.nightbugclub.mcqqauth;

import com.google.gson.JsonObject;
import org.bukkit.Server;
import org.bukkit.command.CommandException;
import org.bukkit.command.RemoteConsoleCommandSender;
import org.bukkit.configuration.file.YamlConfiguration;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Set;

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
                () -> new MohistCommandFeedback.Capture(console, () -> ""));
        when(server.dispatchCommand(console, "minecraft:whitelist add Steve Alex")).thenReturn(true);
        when(server.dispatchCommand(console, "example:inspect world \"夜间 测试\"")).thenReturn(true);
        assertEquals("ok", commands.execute("whitelist add Steve Alex").status());
        assertEquals("ok", commands.execute("example:inspect world \"夜间 测试\"").status());
        verify(server).dispatchCommand(console, "minecraft:whitelist add Steve Alex");
        verify(server).dispatchCommand(console, "example:inspect world \"夜间 测试\"");
    }

    @Test
    void rootPrefixIncludesExactCommandAndEverySubcommand() {
        var commands = new SudoCommands(server, Set.of("whitelist"),
                () -> new MohistCommandFeedback.Capture(console, () -> ""));
        when(server.dispatchCommand(eq(console), anyString())).thenReturn(true);
        for (String input : Set.of("whitelist", "whitelist on", "whitelist remove Steve", "whitelist add Alex")) {
            assertEquals("ok", commands.execute(input).status());
            verify(server).dispatchCommand(console, "minecraft:" + input);
        }
        when(server.getCommandAliases()).thenReturn(Map.of("minecraft:whitelist", new String[]{"first", "second"}));
        clearInvocations(server);
        assertEquals("rejected", commands.execute("whitelist on").status());
        verify(server, never()).dispatchCommand(any(), anyString());
    }

    @Test
    void returnsNativeFeedbackEvenOnDispatchFailureAndIsolatesEachRequest() {
        var commands = new SudoCommands(server, Set.of("list"), () -> {
            var sender = mock(RemoteConsoleCommandSender.class);
            var output = new StringBuilder();
            doAnswer(call -> {
                output.append(call.getArgument(0, String.class));
                return null;
            }).when(sender).sendMessage(anyString());
            return new MohistCommandFeedback.Capture(sender, output::toString);
        });
        when(server.dispatchCommand(any(RemoteConsoleCommandSender.class), eq("minecraft:list")))
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
    void unavailableFeedbackDoesNotDispatch() {
        var commands = new SudoCommands(server, Set.of("list"), () -> {
            throw new IllegalStateException("No compatible feedback source");
        });
        assertEquals(new SudoCommands.Result("error", "服务端命令反馈接口不可用，指令未执行。"),
                commands.execute("list"));
        verify(server, never()).dispatchCommand(any(), anyString());
    }

    @Test
    void retainsFullUtf8BudgetAndTruncatesOnCharacterBoundaryWithNotice() {
        var response = new JsonObject();
        String limit = "😀".repeat(16_384);
        SudoChannel.addOutput(response, limit);
        assertEquals(limit, response.get("output").getAsString());
        assertFalse(response.has("truncated"));

        SudoChannel.addOutput(response, limit + " extra");
        String output = response.get("output").getAsString();
        assertTrue(response.get("truncated").getAsBoolean());
        assertTrue(output.endsWith("\n[输出超过 64 KiB，已截断。]\n"));
        assertTrue(output.getBytes(StandardCharsets.UTF_8).length <= 65_536);
        assertFalse(output.contains("�"));
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
