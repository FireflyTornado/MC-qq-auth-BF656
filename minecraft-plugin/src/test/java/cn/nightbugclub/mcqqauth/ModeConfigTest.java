package cn.nightbugclub.mcqqauth;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.AclFileAttributeView;
import java.nio.file.attribute.PosixFileAttributeView;

import static cn.nightbugclub.mcqqauth.PluginSettings.Mode.ENFORCE;
import static cn.nightbugclub.mcqqauth.PluginSettings.Mode.OBSERVE;
import static org.junit.jupiter.api.Assertions.*;

class ModeConfigTest {
    @TempDir
    Path directory;

    @Test
    void replacesOnlyTheModeValueAndRetainsPermissionsBomAndCrlf() throws Exception {
        String yaml = "\uFEFF# 模式说明 🐱\r\n"
                + "'mode' : \" observe \"  # 观察模式\r\n"
                + "api: {token: sample-test-only, snapshot-url: 'https://localhost/snapshot'}\r\n"
                + "observe:\r\n  notice:\r\n    message:\r\n      - '玩家 {player}，群 {group}'\r\n"
                + "other:\r\n  mode: OBSERVE";
        Path config = config(yaml);
        var posix = Files.getFileAttributeView(config, PosixFileAttributeView.class);
        var acl = Files.getFileAttributeView(config, AclFileAttributeView.class);
        var permissions = posix == null ? null : posix.readAttributes().permissions();
        var entries = acl == null ? null : acl.getAcl();
        var owner = Files.getOwner(config);

        assertTrue(ModeConfig.save(config, ENFORCE, () -> true));
        assertEquals(yaml.replace("\" observe \"", "\" ENFORCE \""), Files.readString(config));
        var reloaded = ModeConfig.load(Files.readString(config));
        assertEquals(ENFORCE, PluginSettings.readMode(reloaded));
        assertEquals("sample-test-only", reloaded.getString("api.token"));
        assertEquals("玩家 Alex，群 未配置", NoticeSettings.read(reloaded).messageFor("Alex").get(0));
        assertEquals(owner, Files.getOwner(config));
        if (posix != null) {
            assertEquals(permissions, posix.readAttributes().permissions());
        }
        if (acl != null) {
            assertEquals(entries, acl.getAcl());
        }
    }

    @Test
    void insertsMissingModeWithoutChangingNestedFieldsOrBlockScalars() throws Exception {
        String yaml = "# legacy\r\napi:\r\n  mode: ENFORCE\r\n  token: example\r\n"
                + "text: |-\r\n  mode: ENFORCE\r\n...\r\n";
        Path config = config(yaml);
        assertEquals(ENFORCE, PluginSettings.readMode(ModeConfig.load(yaml)));
        assertTrue(ModeConfig.save(config, ENFORCE, () -> true));
        assertEquals(yaml, Files.readString(config));
        assertTrue(ModeConfig.save(config, OBSERVE, () -> true));
        assertEquals(yaml.replace("api:", "mode: OBSERVE\r\napi:"), Files.readString(config));
        assertEquals(OBSERVE, PluginSettings.readMode(ModeConfig.load(Files.readString(config))));
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "mode: OTHER", "mode:", "mode: false", "mode: [OBSERVE]",
            "mode: OBSERVE\n\"mode\": ENFORCE",
            "shared: &m OBSERVE\nmode: *m",
            "mode: &m OBSERVE\nother: *m",
            "base: &base {mode: ENFORCE}\n<<: *base",
            "mode: OBSERVE\nmode.child: surprise"
    })
    void rejectsUnsafeRewritesWithoutChangingOriginal(String yaml) throws Exception {
        Path config = config(yaml);
        assertThrows(IllegalArgumentException.class, () -> ModeConfig.save(config, ENFORCE, () -> true));
        assertEquals(yaml, Files.readString(config));
    }

    @Test
    void refusesCommitWhenReadinessDropsAndRemovesTemporaryFile() throws Exception {
        Path config = config("mode: OBSERVE\n");
        assertFalse(ModeConfig.save(config, ENFORCE, () -> false));
        assertEquals("mode: OBSERVE\n", Files.readString(config));
        try (var files = Files.list(directory)) {
            assertEquals(1, files.count());
        }
    }

    private Path config(String yaml) throws Exception {
        return Files.writeString(directory.resolve("config.yml"), yaml);
    }
}
