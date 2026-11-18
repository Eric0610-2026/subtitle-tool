using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string manifest = Path.Combine(root, "package.json");
        string electron = Path.Combine(root, "node_modules", "electron", "dist", "electron.exe");
        try
        {
            if (!File.Exists(manifest))
                throw new FileNotFoundException("未找到 package.json，请将启动器放在项目根目录中。", manifest);
            if (!File.Exists(electron))
                throw new FileNotFoundException("请在项目目录执行 npm ci 和 npx install-electron，再启动字幕工具。", electron);

            Process.Start(new ProcessStartInfo
            {
                FileName = electron,
                Arguments = ".",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "字幕工具启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

}
