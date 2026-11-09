package cn.nightbugclub.mcqqauth;

import org.bukkit.configuration.file.YamlConfiguration;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class PluginSettingsTest {
    @Test
    void missingModeIgnoresBundledObserveDefault() {
        YamlConfiguration defaults = new YamlConfiguration();
        defaults.set("mode", "OBSERVE");
        YamlConfiguration old = new YamlConfiguration();
        old.setDefaults(defaults);
        assertEquals(PluginSettings.Mode.ENFORCE, PluginSettings.readMode(old));
    }

    @Test
    void acceptsBothModesWithNormalizedCase() {
        YamlConfiguration config = new YamlConfiguration();
        config.set("mode", " observe ");
        assertEquals(PluginSettings.Mode.OBSERVE, PluginSettings.readMode(config));
        config.set("mode", "ENFORCE");
        assertEquals(PluginSettings.Mode.ENFORCE, PluginSettings.readMode(config));
    }

    @ParameterizedTest
    @ValueSource(strings = {"mode: TEST", "mode: true", "mode: 1", "mode: [OBSERVE]", "mode: ''"})
    void rejectsInvalidModes(String yaml) throws Exception {
        YamlConfiguration config = new YamlConfiguration();
        config.loadFromString(yaml);
        assertThrows(IllegalArgumentException.class, () -> PluginSettings.readMode(config));
    }

    @Test
    void usesBoundedDefaultsForInvalidNoticeSettings() {
        YamlConfiguration config = new YamlConfiguration();
        config.set("observe.notice.enabled", "false");
        config.set("observe.notice.group-number", 123456789012L);
        config.set("observe.notice.show-delay-ticks", -1);
        config.set("observe.notice.cooldown-seconds", Long.MAX_VALUE);
        config.set("observe.notice.title", "长".repeat(33));
        config.set("observe.notice.message", List.of("长".repeat(81)));
        config.set("observe.notice.confirm-button", "一\n二");
        NoticeSettings settings = NoticeSettings.read(config);
        assertTrue(settings.enabled());
        assertEquals("未配置", settings.groupLabel());
        assertEquals(30, settings.delayTicks());
        assertEquals(86_400, settings.cooldownSeconds());
        assertEquals("社区认证提醒", settings.titleFor("Steve"));
        assertEquals("我知道了，继续游戏", settings.buttonFor("Steve"));
        assertEquals(2, settings.messageFor("Steve").size());
        assertTrue(settings.messageFor("Steve").get(1).contains("/绑定 Steve"));

        config.set("observe.notice.group-number", "12345\n67890");
        assertEquals("未配置", NoticeSettings.read(config).groupLabel());
        config.set("observe.notice.message", List.of("1", "2", "3", "4", "5", "6", "7"));
        assertEquals(2, NoticeSettings.read(config).messageFor("Steve").size());
    }

    @Test
    void rendersOnlySupportedPlaceholdersAndBoundsExpandedText() {
        YamlConfiguration config = new YamlConfiguration();
        config.set("observe.notice.group-number", "123456789012");
        config.set("observe.notice.title", "{group}".repeat(4));
        config.set("observe.notice.message", List.of("玩家 {player}，群 {group}，${1+2} {other}"));
        config.set("observe.notice.confirm-button", "确认 {player}");
        NoticeSettings settings = NoticeSettings.read(config);
        assertEquals("123456789012", settings.groupLabel());
        assertEquals(32, settings.titleFor("Steve").length());
        assertEquals("确认 Steve", settings.buttonFor("Steve"));
        assertEquals(List.of("玩家 Steve，群 123456789012，${1+2} {other}"), settings.messageFor("Steve"));
    }
}
