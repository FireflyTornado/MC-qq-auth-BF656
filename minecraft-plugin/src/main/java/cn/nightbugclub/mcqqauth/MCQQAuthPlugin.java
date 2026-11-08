
package cn.nightbugclub.mcqqauth;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.plugin.java.JavaPlugin;

public final class MCQQAuthPlugin extends JavaPlugin {

    private String mode;

    @Override
    public void onEnable() {
        saveDefaultConfig();

        mode = getConfig().getString("mode", "OBSERVE");

        getLogger().info("MCQQAuth v" + getDescription().getVersion());
        getLogger().info("Running mode: " + mode);
        getLogger().info("Plugin initialized successfully.");

        // 本阶段仅验证插件加载。
        // 暂不执行任何玩家准入控制。
    }

    @Override
    public void onDisable() {
        getLogger().info("MCQQAuth disabled.");
    }

    @Override
    public boolean onCommand(
            CommandSender sender,
            Command command,
            String label,
            String[] args
    ) {
        if (command.getName().equalsIgnoreCase("mcqqauth")) {
            sender.sendMessage("MCQQAuth v" + getDescription().getVersion());
            sender.sendMessage("Mode: " + mode);
            sender.sendMessage("Authorization enforcement: inactive");
            return true;
        }

        return false;
    }
}
