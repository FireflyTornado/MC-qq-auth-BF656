package cn.nightbugclub.mcqqauth;

import org.bukkit.Server;
import org.bukkit.command.RemoteConsoleCommandSender;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Type;

import java.lang.invoke.MethodHandles;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketAddress;
import java.util.Objects;
import java.util.Optional;
import java.util.function.Consumer;
import java.util.function.Supplier;

import static org.objectweb.asm.Opcodes.*;

final class MohistCommandFeedback {
    record Capture(RemoteConsoleCommandSender sender, Supplier<BoundedCommandOutput.Snapshot> output) {}

    private record Access(Constructor<?> constructor, Method createStack, Method getSender,
                          Method visit, Class<?> visitorType, Object handle) {}

    private final Server server;
    private Access access;

    MohistCommandFeedback(Server server) {
        this.server = server;
    }

    Capture open() {
        try {
            if (access == null) {
                access = resolve();
            }
            var output = new BoundedCommandOutput();
            Access methods = access;
            Object visitor = Proxy.newProxyInstance(methods.visitorType.getClassLoader(),
                    new Class<?>[]{methods.visitorType}, (proxy, method, args) ->
                            output.append((String) args[0]) ? Optional.of(Boolean.TRUE) : Optional.empty());
            Consumer<Object> appendComponent = component -> {
                if (!output.truncated()) {
                    visit(methods.visit, component, visitor);
                    output.append("\n");
                }
            };
            Consumer<String> appendString = output::append;
            // The address is metadata for the sender. No socket or shared RCON session is opened.
            Object source = methods.constructor.newInstance(methods.handle,
                    new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), appendComponent, appendString);
            var sender = Objects.requireNonNull(
                    (RemoteConsoleCommandSender) methods.getSender.invoke(source, methods.createStack.invoke(source)));
            return new Capture(sender, output::snapshot);
        } catch (ReflectiveOperationException | LinkageError | ClassCastException | NullPointerException e) {
            throw new IllegalStateException("Mohist command feedback is unavailable", e);
        }
    }

    private Access resolve() throws ReflectiveOperationException {
        ClassLoader loader = server.getClass().getClassLoader();
        Class<?> minecraftServer = loader.loadClass("net.minecraft.server.MinecraftServer");
        Class<?> source = loader.loadClass("net.minecraft.server.rcon.RconConsoleSource");
        Class<?> component = loader.loadClass("net.minecraft.network.chat.Component");
        Class<?> visitor = loader.loadClass("net.minecraft.network.chat.FormattedText$ContentConsumer");
        Method createStack = mappedMethod(source, "createCommandSourceStack", "m_11514_");
        Method getSender = source.getMethod("getBukkitSender", createStack.getReturnType());
        Method visit = mappedMethod(component, "visit", "m_5651_", visitor);
        Method sendComponent = mappedMethod(source, "sendSystemMessage", "m_213846_", component);
        Method sendString = source.getMethod("sendMessage", String.class);
        Object handle = server.getClass().getMethod("getServer").invoke(server);
        return new Access(createSource(source, minecraftServer, sendComponent, sendString),
                createStack, getSender, visit, visitor, handle);
    }

    private static Method mappedMethod(Class<?> source, String named, String srg, Class<?>... args)
            throws NoSuchMethodException {
        try {
            return source.getMethod(named, args);
        } catch (NoSuchMethodException e) {
            return source.getMethod(srg, args);
        }
    }

    private static void visit(Method visit, Object component, Object visitor) {
        try {
            visit.invoke(component, visitor);
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("Unable to read Mohist command feedback", e);
        }
    }

    // Keep the CraftBukkit sender and override the two methods that normally append to its unbounded buffer.
    // Resolve names at runtime because Mohist can expose Mojang or SRG names.
    static Constructor<?> createSource(Class<?> source, Class<?> server, Method sendComponent, Method sendString)
            throws ReflectiveOperationException {
        String name = Type.getInternalName(MohistCommandFeedback.class) + "$Source";
        String parent = Type.getInternalName(source);
        String consumer = Type.getDescriptor(Consumer.class);
        String parentConstructor = Type.getMethodDescriptor(Type.VOID_TYPE, Type.getType(server),
                Type.getType(SocketAddress.class));
        String constructor = Type.getMethodDescriptor(Type.VOID_TYPE, Type.getType(server),
                Type.getType(SocketAddress.class), Type.getType(Consumer.class), Type.getType(Consumer.class));
        var writer = new ClassWriter(ClassWriter.COMPUTE_MAXS);
        writer.visit(V17, ACC_FINAL | ACC_SUPER, name, null, parent, null);
        writer.visitField(ACC_PRIVATE | ACC_FINAL, "component", consumer, null, null).visitEnd();
        writer.visitField(ACC_PRIVATE | ACC_FINAL, "text", consumer, null, null).visitEnd();
        var init = writer.visitMethod(ACC_PUBLIC, "<init>", constructor, null, null);
        init.visitCode();
        init.visitVarInsn(ALOAD, 0);
        init.visitVarInsn(ALOAD, 1);
        init.visitVarInsn(ALOAD, 2);
        init.visitMethodInsn(INVOKESPECIAL, parent, "<init>", parentConstructor, false);
        init.visitVarInsn(ALOAD, 0);
        init.visitVarInsn(ALOAD, 3);
        init.visitFieldInsn(PUTFIELD, name, "component", consumer);
        init.visitVarInsn(ALOAD, 0);
        init.visitVarInsn(ALOAD, 4);
        init.visitFieldInsn(PUTFIELD, name, "text", consumer);
        init.visitInsn(RETURN);
        init.visitMaxs(0, 0);
        init.visitEnd();
        overrideOutput(writer, name, sendComponent, "component");
        overrideOutput(writer, name, sendString, "text");
        writer.visitEnd();
        return MethodHandles.lookup().defineHiddenClass(writer.toByteArray(), true).lookupClass()
                .getConstructor(server, SocketAddress.class, Consumer.class, Consumer.class);
    }

    private static void overrideOutput(ClassWriter writer, String owner, Method original, String field) {
        var method = writer.visitMethod(ACC_PUBLIC, original.getName(), Type.getMethodDescriptor(original), null, null);
        method.visitCode();
        method.visitVarInsn(ALOAD, 0);
        method.visitFieldInsn(GETFIELD, owner, field, Type.getDescriptor(Consumer.class));
        method.visitVarInsn(ALOAD, 1);
        method.visitMethodInsn(INVOKEINTERFACE, Type.getInternalName(Consumer.class),
                "accept", "(Ljava/lang/Object;)V", true);
        method.visitInsn(RETURN);
        method.visitMaxs(0, 0);
        method.visitEnd();
    }
}
