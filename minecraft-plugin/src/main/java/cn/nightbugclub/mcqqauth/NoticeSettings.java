package cn.nightbugclub.mcqqauth;

import org.bukkit.configuration.ConfigurationSection;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

record NoticeSettings(boolean enabled, int delayTicks, int cooldownSeconds, String groupNumber,
                      String title, List<String> message, String confirmButton) {
    private static final String PREFIX = "observe.notice.";
    private static final Pattern GROUP_NUMBER = Pattern.compile("[1-9][0-9]{4,19}");
    private static final int LABEL_LENGTH = 32;
    private static final int LINE_LENGTH = 80;
    private static final List<String> DEFAULT_MESSAGE = List.of(
            "你的 Minecraft 游戏 ID 尚未完成社区认证。",
            "请加入服务器 QQ 群，并发送 /绑定 {player} 完成绑定。");

    static NoticeSettings read(ConfigurationSection config) {
        Object group = config.get(PREFIX + "group-number");
        String groupNumber = group instanceof String text && GROUP_NUMBER.matcher(text).matches() ? text : "";
        return new NoticeSettings(config.getBoolean(PREFIX + "enabled", true),
                integer(config, "show-delay-ticks", 30, 1, 1_200),
                integer(config, "cooldown-seconds", 86_400, 0, 2_592_000),
                groupNumber,
                label(config, "title", "社区认证提醒"),
                message(config.get(PREFIX + "message")),
                label(config, "confirm-button", "我知道了，继续游戏"));
    }

    String groupLabel() {
        return groupNumber.isEmpty() ? "未配置" : groupNumber;
    }

    String titleFor(String player) {
        return render(title, player, LABEL_LENGTH);
    }

    String buttonFor(String player) {
        return render(confirmButton, player, LABEL_LENGTH);
    }

    List<String> messageFor(String player) {
        return message.stream().map(line -> render(line, player, LINE_LENGTH)).toList();
    }

    private String render(String template, String player, int limit) {
        String text = template.replace("{group}", groupLabel()).replace("{player}", player);
        return text.codePointCount(0, text.length()) <= limit ? text
                : text.substring(0, text.offsetByCodePoints(0, limit));
    }

    private static int integer(ConfigurationSection config, String key, int fallback, int min, int max) {
        Object raw = config.get(PREFIX + key);
        if (raw instanceof Number number && (number instanceof Integer || number instanceof Long)) {
            long value = number.longValue();
            if (value >= min && value <= max) {
                return (int) value;
            }
        }
        return fallback;
    }

    private static String label(ConfigurationSection config, String key, String fallback) {
        Object value = config.get(PREFIX + key);
        return value instanceof String text && valid(text, LABEL_LENGTH) ? text : fallback;
    }

    private static List<String> message(Object value) {
        if (!(value instanceof List<?> lines) || lines.isEmpty() || lines.size() > 6) {
            return DEFAULT_MESSAGE;
        }
        List<String> result = new ArrayList<>();
        for (Object line : lines) {
            if (!(line instanceof String text) || !valid(text, LINE_LENGTH)) {
                return DEFAULT_MESSAGE;
            }
            result.add(text);
        }
        return List.copyOf(result);
    }

    private static boolean valid(String text, int limit) {
        return !text.isBlank() && text.codePointCount(0, text.length()) <= limit
                && text.codePoints().noneMatch(Character::isISOControl);
    }
}
