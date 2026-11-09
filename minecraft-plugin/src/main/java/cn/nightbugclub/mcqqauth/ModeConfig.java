package cn.nightbugclub.mcqqauth;

import org.bukkit.configuration.InvalidConfigurationException;
import org.bukkit.configuration.file.YamlConfiguration;
import org.yaml.snakeyaml.Yaml;
import org.yaml.snakeyaml.nodes.MappingNode;
import org.yaml.snakeyaml.nodes.NodeTuple;
import org.yaml.snakeyaml.nodes.ScalarNode;
import org.yaml.snakeyaml.nodes.Tag;

import java.io.IOException;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.AclEntry;
import java.nio.file.attribute.AclFileAttributeView;
import java.nio.file.attribute.DosFileAttributeView;
import java.nio.file.attribute.FileAttribute;
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.List;
import java.util.function.BooleanSupplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.yaml.snakeyaml.DumperOptions.FlowStyle.BLOCK;

final class ModeConfig {
    private static final Pattern LINE_BREAK = Pattern.compile("\\r\\n|[\\r\\n]");
    private static final String UNSAFE = "config.yml 的 mode 无法安全更新，请检查顶层字段的 YAML 格式。";

    private record Document(MappingNode root, NodeTuple mode) {}

    private ModeConfig() {}

    static YamlConfiguration load(String yaml) throws InvalidConfigurationException {
        inspect(withoutBom(yaml));
        return loadYaml(withoutBom(yaml));
    }

    // Returns false when authorization became unavailable before the commit point.
    static boolean save(Path file, PluginSettings.Mode mode, BooleanSupplier canCommit)
            throws IOException, InvalidConfigurationException {
        if (!Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("config.yml is not a regular file");
        }
        String original = Files.readString(file, StandardCharsets.UTF_8);
        String yaml = withoutBom(original);
        Document document = inspect(yaml);
        String updated = document.mode == null ? insertMode(yaml, document.root, mode)
                : replaceMode(yaml, document.mode, mode);
        if (PluginSettings.readMode(load(updated)) != mode) {
            throw new IllegalArgumentException(UNSAFE);
        }
        if (updated.equals(yaml)) {
            return canCommit.getAsBoolean();
        }
        if (!Files.isWritable(file)) {
            throw new IOException("config.yml is not writable");
        }
        Path temporary = createTemporaryFile(file);
        boolean committed = false;
        try {
            String bom = original.length() == yaml.length() ? "" : "\uFEFF";
            try (FileChannel channel = FileChannel.open(temporary, StandardOpenOption.WRITE)) {
                ByteBuffer content = StandardCharsets.UTF_8.encode(bom + updated);
                while (content.hasRemaining()) {
                    channel.write(content);
                }
                channel.force(true);
            }
            if (!Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS)
                    || !original.equals(Files.readString(file, StandardCharsets.UTF_8))) {
                throw new IllegalArgumentException("config.yml 在保存期间被修改，请重试。");
            }
            if (!canCommit.getAsBoolean()) {
                return false;
            }
            Files.move(temporary, file, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            committed = true;
            return true;
        } finally {
            if (!committed) {
                Files.deleteIfExists(temporary);
            }
        }
    }

    private static YamlConfiguration loadYaml(String yaml) throws InvalidConfigurationException {
        YamlConfiguration config = new YamlConfiguration() {
            @Override
            public void set(String path, Object value) {
                // Bukkit drops YAML nulls by default. Keep a blank mode invalid.
                super.set(path, "mode".equals(path) && value == null ? "" : value);
            }
        };
        config.loadFromString(yaml);
        return config;
    }

    private static Document inspect(String yaml) {
        var node = new Yaml().compose(new StringReader(yaml));
        if (node == null) {
            return new Document(null, null);
        }
        if (!(node instanceof MappingNode root)) {
            throw new IllegalArgumentException(UNSAFE);
        }
        NodeTuple mode = null;
        for (NodeTuple field : root.getValue()) {
            if (field.getKeyNode() instanceof ScalarNode key
                    && Tag.STR.equals(key.getTag()) && key.getValue().equals("mode")) {
                if (mode != null) {
                    throw new IllegalArgumentException("config.yml 包含重复的顶层 mode，请只保留一项。");
                }
                mode = field;
            }
        }
        return new Document(root, mode);
    }

    private static PluginSettings.Mode readMode(NodeTuple field) {
        return PluginSettings.parseMode(field != null && field.getValueNode() instanceof ScalarNode value
                && Tag.STR.equals(value.getTag()) ? value.getValue() : null);
    }

    private static String replaceMode(String yaml, NodeTuple field, PluginSettings.Mode mode) {
        if (readMode(field) == mode) {
            return yaml;
        }
        var key = (ScalarNode) field.getKeyNode();
        var value = (ScalarNode) field.getValueNode();
        int keyEnd = yaml.offsetByCodePoints(0, key.getEndMark().getIndex());
        int start = yaml.offsetByCodePoints(0, value.getStartMark().getIndex());
        int end = yaml.offsetByCodePoints(0, value.getEndMark().getIndex());
        String scalar = yaml.substring(start, end);
        String quote = switch (value.getScalarStyle()) {
            case PLAIN -> "";
            case SINGLE_QUOTED -> "'";
            case DOUBLE_QUOTED -> "\"";
            default -> throw new IllegalArgumentException(UNSAFE);
        };
        if (keyEnd > start || !yaml.substring(keyEnd, start).matches("[ \\t]*:[ \\t]*")
                || !scalar.matches(quote + "[ \\t]*(?i:OBSERVE|ENFORCE)[ \\t]*" + quote)) {
            throw new IllegalArgumentException(UNSAFE);
        }
        Matcher name = Pattern.compile("(?i:OBSERVE|ENFORCE)").matcher(scalar);
        return yaml.substring(0, start) + name.replaceFirst(mode.name()) + yaml.substring(end);
    }

    private static String insertMode(String yaml, MappingNode root, PluginSettings.Mode mode) {
        if (root != null && root.isMerged()) {
            throw new IllegalArgumentException(UNSAFE);
        }
        if (mode == PluginSettings.Mode.ENFORCE) {
            return yaml; // An absent mode already persists the legacy default.
        }
        Matcher lines = LINE_BREAK.matcher(yaml);
        String newline = lines.find() ? lines.group() : "\n";
        if (root == null) {
            String separator = yaml.isEmpty() || yaml.endsWith("\n") || yaml.endsWith("\r") ? "" : newline;
            String ending = yaml.isEmpty() || !separator.isEmpty() ? "" : newline;
            return yaml + separator + "mode: " + mode + ending;
        }
        if (root.getFlowStyle() != BLOCK || root.getValue().isEmpty()) {
            throw new IllegalArgumentException(UNSAFE);
        }
        int start = yaml.offsetByCodePoints(0, root.getStartMark().getIndex());
        int lineStart = Math.max(yaml.lastIndexOf('\n', start - 1), yaml.lastIndexOf('\r', start - 1)) + 1;
        String indent = yaml.substring(lineStart, start);
        if (!indent.matches(" *")) {
            throw new IllegalArgumentException(UNSAFE);
        }
        return yaml.substring(0, start) + "mode: " + mode + newline + indent + yaml.substring(start);
    }

    private static String withoutBom(String yaml) {
        return yaml.startsWith("\uFEFF") ? yaml.substring(1) : yaml;
    }

    private static Path createTemporaryFile(Path file) throws IOException {
        PosixFileAttributeView posix = Files.getFileAttributeView(file, PosixFileAttributeView.class);
        if (posix != null) {
            var attributes = posix.readAttributes();
            Path temporary = Files.createTempFile(file.getParent(), ".mcqqauth-", ".tmp",
                    PosixFilePermissions.asFileAttribute(attributes.permissions()));
            try {
                var target = Files.getFileAttributeView(temporary, PosixFileAttributeView.class);
                if (!attributes.owner().equals(target.getOwner())) {
                    target.setOwner(attributes.owner());
                }
                if (!attributes.group().equals(target.readAttributes().group())) {
                    target.setGroup(attributes.group());
                }
                target.setPermissions(attributes.permissions());
                return temporary;
            } catch (IOException | RuntimeException e) {
                Files.deleteIfExists(temporary);
                throw e;
            }
        }
        AclFileAttributeView acl = Files.getFileAttributeView(file, AclFileAttributeView.class);
        DosFileAttributeView dos = Files.getFileAttributeView(file, DosFileAttributeView.class);
        if (acl == null || (dos != null && dos.readAttributes().isReadOnly())) {
            throw new IOException("Cannot preserve config.yml permissions");
        }
        List<AclEntry> entries = acl.getAcl();
        Path temporary = Files.createTempFile(file.getParent(), ".mcqqauth-", ".tmp", new FileAttribute<>() {
            @Override public String name() { return "acl:acl"; }
            @Override public List<AclEntry> value() { return entries; }
        });
        try {
            var target = Files.getFileAttributeView(temporary, AclFileAttributeView.class);
            if (!acl.getOwner().equals(target.getOwner())) {
                target.setOwner(acl.getOwner());
            }
            if (!entries.equals(target.getAcl())) {
                throw new IOException("Cannot preserve config.yml ACL");
            }
            return temporary;
        } catch (IOException | RuntimeException e) {
            Files.deleteIfExists(temporary);
            throw e;
        }
    }
}
