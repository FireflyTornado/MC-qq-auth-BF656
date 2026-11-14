package cn.nightbugclub.mcqqauth;

import org.bukkit.OfflinePlayer;
import org.bukkit.Server;
import org.bukkit.entity.Player;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;
import java.util.regex.Pattern;

final class SudoCommands {
    static final String ALLOWLIST_FILE = "sudo-allowlist.txt";
    private static final Pattern COMMAND_LABEL = Pattern.compile("[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?");
    private static final Pattern WHITELIST_ADD = Pattern.compile("minecraft:whitelist add ([A-Za-z0-9_]{1,16})");

    record Result(String status, String output) {
        Result(String status) {
            this(status, "");
        }
    }

    private final Server server;
    // Reload and dispatch are serialized on the Minecraft main thread.
    private Set<String> allowed;
    private boolean dispatching;

    SudoCommands(Server server, Set<String> allowed) {
        this.server = server;
        this.allowed = Set.copyOf(allowed);
    }

    int reloadAllowlist(Path file) throws IOException {
        try {
            allowed = readAllowlist(file);
        } catch (IOException | IllegalArgumentException | SecurityException e) {
            allowed = Set.of();
            throw e;
        }
        return allowed.size();
    }

    boolean isDispatching() {
        return dispatching;
    }

    static Set<String> readAllowlist(Path file) throws IOException {
        var prefixes = new HashSet<String>();
        try (var reader = Files.newBufferedReader(file)) {
            int lineNumber = 0;
            for (String line; (line = reader.readLine()) != null; ) {
                if (++lineNumber == 1 && line.startsWith("\uFEFF")) {
                    line = line.substring(1);
                }
                String prefix = line.strip();
                if (prefix.isEmpty() || prefix.startsWith("#")) {
                    continue;
                }
                if (!validCommand(prefix) || !COMMAND_LABEL.matcher(label(prefix)).matches()) {
                    throw new IllegalArgumentException(ALLOWLIST_FILE + " 第 " + lineNumber + " 行的命令前缀无效");
                }
                prefixes.add(prefix);
            }
        }
        return Set.copyOf(prefixes);
    }

    Result execute(String command) {
        if (!validCommand(command) || allowed.stream().noneMatch(
                prefix -> command.equals(prefix) || command.startsWith(prefix + " "))) {
            return new Result("rejected");
        }

        String qualified = label(command).contains(":") ? command : "minecraft:" + command;
        String target = label(qualified);
        // Configured Bukkit aliases can expand to multiple commands.
        if (server.getCommandAliases().keySet().stream().anyMatch(target::equalsIgnoreCase)) {
            return new Result("rejected");
        }
        dispatching = true;
        try {
            if (!server.dispatchCommand(server.getConsoleSender(), qualified)) {
                return new Result("error", "服务端未找到该命令。");
            }
            return feedback(qualified);
        } finally {
            dispatching = false;
        }
    }

    private Result feedback(String dispatched) {
        if (dispatched.equals("minecraft:list")) {
            var names = server.getOnlinePlayers().stream().map(Player::getName).sorted().toList();
            return new Result("ok", "在线玩家 " + names.size() + "/" + server.getMaxPlayers()
                    + (names.isEmpty() ? "" : "：" + String.join(", ", names)));
        }
        var add = WHITELIST_ADD.matcher(dispatched);
        if (add.matches()) {
            String name = add.group(1);
            boolean listed = server.getWhitelistedPlayers().stream().map(OfflinePlayer::getName)
                    .anyMatch(name::equalsIgnoreCase);
            return listed ? new Result("ok", name + " 已在白名单中。")
                    : new Result("error", "未能确认 " + name + " 已加入白名单。");
        }
        return new Result("ok", "服务端命令调用已返回。");
    }

    private static String label(String command) {
        int space = command.indexOf(' ');
        return space < 0 ? command : command.substring(0, space);
    }

    private static boolean validCommand(String command) {
        return !command.isEmpty() && command.codePointCount(0, command.length()) <= 256
                && !command.startsWith(" ") && !command.endsWith(" ")
                && command.codePoints().noneMatch(SudoCommands::invalidCharacter);
    }

    private static boolean invalidCharacter(int codePoint) {
        return switch (Character.getType(codePoint)) {
            case Character.CONTROL, Character.FORMAT, Character.SURROGATE, Character.PRIVATE_USE,
                 Character.UNASSIGNED, Character.LINE_SEPARATOR, Character.PARAGRAPH_SEPARATOR -> true;
            default -> false;
        };
    }
}
