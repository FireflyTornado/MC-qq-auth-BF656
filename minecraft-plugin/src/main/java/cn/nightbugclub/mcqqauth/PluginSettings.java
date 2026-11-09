package cn.nightbugclub.mcqqauth;

import org.bukkit.configuration.ConfigurationSection;

import java.net.URI;
import java.util.Locale;
import java.util.regex.Pattern;

final class PluginSettings {
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9._~+/-]+=*");

    enum Mode { OBSERVE, ENFORCE }

    final URI snapshotUrl;
    final URI websocketUrl;
    final String token;
    final int connectTimeoutMs;
    final int requestTimeoutMs;
    final int reconnectMinMs;
    final int reconnectMaxMs;

    PluginSettings(URI snapshotUrl, URI websocketUrl, String token,
                   int connectTimeoutMs, int requestTimeoutMs, int reconnectMinMs, int reconnectMaxMs) {
        this.snapshotUrl = secureUrl(snapshotUrl, "https", "api.snapshot-url");
        this.websocketUrl = secureUrl(websocketUrl, "wss", "api.websocket-url");
        if (token == null || token.length() > 4096 || !TOKEN.matcher(token).matches()
                || token.equals("REPLACE_WITH_MC_API_TOKEN")) {
            throw new IllegalArgumentException("api.token 需要填写有效的 MC_API_TOKEN");
        }
        this.token = token;
        this.connectTimeoutMs = positive(connectTimeoutMs, "api.connect-timeout-ms");
        this.requestTimeoutMs = positive(requestTimeoutMs, "api.request-timeout-ms");
        this.reconnectMinMs = positive(reconnectMinMs, "sync.reconnect-min-ms");
        if (reconnectMaxMs < reconnectMinMs || reconnectMaxMs > 300_000) {
            throw new IllegalArgumentException("sync.reconnect-max-ms 必须不小于初始等待且最多为 300000");
        }
        this.reconnectMaxMs = reconnectMaxMs;
    }

    static PluginSettings read(ConfigurationSection config) {
        return new PluginSettings(
                parseUrl(config, "api.snapshot-url"),
                parseUrl(config, "api.websocket-url"),
                config.isString("api.token") ? config.getString("api.token") : null,
                config.getInt("api.connect-timeout-ms"),
                config.getInt("api.request-timeout-ms"),
                config.getInt("sync.reconnect-min-ms"),
                config.getInt("sync.reconnect-max-ms")
        );
    }

    static Mode readMode(ConfigurationSection config) {
        // Ignore the bundled template default for existing installations.
        Object value = config.get("mode", Mode.ENFORCE.name());
        if (value instanceof String name) {
            try {
                return Mode.valueOf(name.strip().toUpperCase(Locale.ROOT));
            } catch (IllegalArgumentException ignored) {
                // Report the invalid field without echoing its value.
            }
        }
        throw new IllegalArgumentException("mode 必须为 OBSERVE 或 ENFORCE");
    }

    private static URI parseUrl(ConfigurationSection config, String key) {
        try {
            return URI.create(config.getString(key, ""));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(key + " 不是有效的 URL");
        }
    }

    private static URI secureUrl(URI url, String scheme, String key) {
        if (!scheme.equalsIgnoreCase(url.getScheme()) || url.getHost() == null || url.getUserInfo() != null
                || url.getPort() == 0 || url.getPort() > 65535
                || url.getRawQuery() != null || url.getRawFragment() != null || url.getRawPath().isEmpty()) {
            throw new IllegalArgumentException(key + " 需要使用 " + scheme + " 并指定主机和接口路径");
        }
        return url;
    }

    private static int positive(int value, String key) {
        if (value <= 0 || value > 300_000) {
            throw new IllegalArgumentException(key + " 必须在 1 到 300000 毫秒之间");
        }
        return value;
    }
}
