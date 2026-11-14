package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.command.CommandException;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;
import java.util.function.Supplier;
import java.util.regex.Pattern;

final class SudoCommands {
    static final String ALLOWLIST_FILE = "sudo-allowlist.txt";
    private static final Pattern COMMAND_LABEL = Pattern.compile("[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?");

    record Result(String status, String output) {
        Result(String status) {
            this(status, "");
        }
    }

    private final Server server;
    private final Supplier<MohistCommandFeedback.Capture> openFeedback;
    // Reload and dispatch are serialized on the Minecraft main thread.
    private Set<String> allowed;
    private boolean dispatching;

    SudoCommands(Server server, Set<String> allowed) {
        this(server, allowed, new MohistCommandFeedback(server)::open);
    }

    SudoCommands(Server server, Set<String> allowed, Supplier<MohistCommandFeedback.Capture> openFeedback) {
        this.server = server;
        this.allowed = Set.copyOf(allowed);
        this.openFeedback = openFeedback;
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
        MohistCommandFeedback.Capture feedback;
        try {
            feedback = openFeedback.get();
        } catch (IllegalStateException e) {
            return new Result("error", "服务端命令反馈接口不可用，指令未执行。");
        }
        dispatching = true;
        try {
            boolean dispatched;
            try {
                dispatched = server.dispatchCommand(feedback.sender(), qualified);
            } catch (CommandException e) {
                String output = feedback.output().get();
                if (output.isBlank()) {
                    throw e;
                }
                return new Result("error", output);
            }
            String output = feedback.output().get();
            if (!dispatched && output.isBlank()) {
                return new Result("error", "服务端未找到该命令。");
            }
            return new Result(dispatched ? "ok" : "error", output);
        } finally {
            dispatching = false;
        }
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
