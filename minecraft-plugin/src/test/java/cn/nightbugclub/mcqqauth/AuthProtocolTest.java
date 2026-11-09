package cn.nightbugclub.mcqqauth;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;

class AuthProtocolTest {
    @Test
    void acceptsEmptyRosterAndSigned64BitNonnegativeVersions() throws IOException {
        var initial = snapshot("{\"version\":0,\"active_names\":[]}");
        assertEquals(0, initial.version());
        assertEquals(Set.of(), initial.activeNames());
        var latest = snapshot("{\"version\":9223372036854775807,\"active_names\":[\"steve\"]}");
        assertEquals(Long.MAX_VALUE, latest.version());
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "", "null", "[]", "{\"version\":1}", "{\"active_names\":[]}",
            "{\"version\":\"1\",\"active_names\":[]}",
            "{\"version\":-1,\"active_names\":[]}",
            "{\"version\":1.0,\"active_names\":[]}",
            "{\"version\":1e3,\"active_names\":[]}",
            "{\"version\":9223372036854775808,\"active_names\":[]}",
            "{\"version\":1,\"version\":2,\"active_names\":[]}",
            "{\"version\":1,\"active_names\":null}",
            "{\"version\":1,\"active_names\":{}}",
            "{\"version\":1,\"active_names\":[\"Steve\"]}",
            "{\"version\":1,\"active_names\":[\"steve\",\"steve\"]}",
            "{\"version\":1,\"active_names\":[42]}",
            "{version:1,active_names:[]}", "{\"version\":1,\"active_names\":[]}{}"
    })
    void rejectsInvalidSnapshot(String json) {
        assertThrows(IOException.class, () -> snapshot(json));
    }

    private static AuthProtocol.Snapshot snapshot(String json) throws IOException {
        return AuthProtocol.snapshot(json.getBytes(StandardCharsets.UTF_8));
    }
}
