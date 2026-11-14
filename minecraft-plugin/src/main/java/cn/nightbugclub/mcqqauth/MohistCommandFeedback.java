package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.command.RemoteConsoleCommandSender;

import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.util.Objects;
import java.util.function.Supplier;

final class MohistCommandFeedback {
    record Capture(RemoteConsoleCommandSender sender, Supplier<String> output) {}

    private final Server server;

    MohistCommandFeedback(Server server) {
        this.server = server;
    }

    Capture open() {
        try {
            ClassLoader loader = server.getClass().getClassLoader();
            Class<?> minecraftServer = loader.loadClass("net.minecraft.server.MinecraftServer");
            Class<?> source = loader.loadClass("net.minecraft.server.rcon.RconConsoleSource");
            Method createStack = mappedMethod(source, "createCommandSourceStack", "m_11514_");
            Method getOutput = mappedMethod(source, "getCommandResponse", "m_11513_");
            Method getSender = source.getMethod("getBukkitSender", createStack.getReturnType());
            Object handle = server.getClass().getMethod("getServer").invoke(server);

            // The address is metadata for the sender. No socket or shared RCON session is opened.
            Object buffer = source.getConstructor(minecraftServer, SocketAddress.class)
                    .newInstance(handle, new InetSocketAddress(InetAddress.getLoopbackAddress(), 0));
            var sender = Objects.requireNonNull(
                    (RemoteConsoleCommandSender) getSender.invoke(buffer, createStack.invoke(buffer)));
            return new Capture(sender, () -> readOutput(getOutput, buffer));
        } catch (ReflectiveOperationException | LinkageError | ClassCastException | NullPointerException e) {
            throw new IllegalStateException("Mohist command feedback is unavailable", e);
        }
    }

    private static Method mappedMethod(Class<?> source, String named, String srg) throws NoSuchMethodException {
        try {
            return source.getMethod(named);
        } catch (NoSuchMethodException e) {
            return source.getMethod(srg);
        }
    }

    private static String readOutput(Method getOutput, Object buffer) {
        try {
            return (String) getOutput.invoke(buffer);
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("Unable to read Mohist command feedback", e);
        }
    }
}
