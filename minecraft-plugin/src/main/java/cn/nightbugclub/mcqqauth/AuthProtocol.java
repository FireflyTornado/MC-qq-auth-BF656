package cn.nightbugclub.mcqqauth;

import com.google.gson.Strictness;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;

import java.io.IOException;
import java.io.StringReader;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

final class AuthProtocol {
    private static final Pattern PLAYER_NAME = Pattern.compile("[A-Za-z0-9_]{1,16}");
    private static final Pattern NAME_KEY = Pattern.compile("[a-z0-9_]{1,16}");
    private static final Pattern VERSION = Pattern.compile("0|[1-9][0-9]*");

    record Snapshot(long version, Set<String> activeNames) {
        Snapshot {
            activeNames = Set.copyOf(activeNames);
        }

        boolean allows(String playerName) {
            return PLAYER_NAME.matcher(playerName).matches()
                    && activeNames.contains(playerName.toLowerCase(Locale.ROOT));
        }
    }

    enum MessageType { CONNECTED, AUTH_UPDATED }

    record Notice(MessageType type, long version) {}

    static Snapshot snapshot(byte[] body) throws IOException {
        String json = StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(body)).toString();
        try (JsonReader reader = reader(json)) {
            long version = -1;
            Set<String> names = null;
            Set<String> fields = new HashSet<>();
            reader.beginObject();
            while (reader.hasNext()) {
                switch (field(reader, fields)) {
                    case "version" -> version = version(reader);
                    case "active_names" -> names = names(reader);
                    default -> reader.skipValue();
                }
            }
            endObject(reader);
            if (version < 0 || names == null) {
                throw invalid();
            }
            return new Snapshot(version, names);
        } catch (IllegalStateException | NumberFormatException e) {
            throw invalid();
        }
    }

    static Notice notice(String json) throws IOException {
        try (JsonReader reader = reader(json)) {
            String type = "";
            long version = -1;
            Set<String> fields = new HashSet<>();
            reader.beginObject();
            while (reader.hasNext()) {
                switch (field(reader, fields)) {
                    case "type" -> type = string(reader);
                    case "version" -> version = version(reader);
                    default -> reader.skipValue();
                }
            }
            endObject(reader);
            if (type.equals("connected")) {
                return new Notice(MessageType.CONNECTED, -1);
            }
            if (type.equals("auth_updated") && version >= 0) {
                return new Notice(MessageType.AUTH_UPDATED, version);
            }
            throw invalid();
        } catch (IllegalStateException | NumberFormatException e) {
            throw invalid();
        }
    }

    private static JsonReader reader(String json) {
        JsonReader reader = new JsonReader(new StringReader(json));
        reader.setStrictness(Strictness.STRICT);
        return reader;
    }

    private static String field(JsonReader reader, Set<String> fields) throws IOException {
        String name = reader.nextName();
        if (!fields.add(name)) {
            throw invalid();
        }
        return name;
    }

    private static void endObject(JsonReader reader) throws IOException {
        reader.endObject();
        if (reader.peek() != JsonToken.END_DOCUMENT) {
            throw invalid();
        }
    }

    private static long version(JsonReader reader) throws IOException {
        if (reader.peek() != JsonToken.NUMBER) {
            throw invalid();
        }
        String value = reader.nextString();
        if (!VERSION.matcher(value).matches()) {
            throw invalid();
        }
        return Long.parseLong(value);
    }

    private static Set<String> names(JsonReader reader) throws IOException {
        Set<String> names = new HashSet<>();
        reader.beginArray();
        while (reader.hasNext()) {
            String name = string(reader);
            if (!NAME_KEY.matcher(name).matches() || !names.add(name)) {
                throw invalid();
            }
        }
        reader.endArray();
        return names;
    }

    private static String string(JsonReader reader) throws IOException {
        if (reader.peek() != JsonToken.STRING) {
            throw invalid();
        }
        return reader.nextString();
    }

    private static IOException invalid() {
        return new IOException("授权协议数据无效");
    }
}
