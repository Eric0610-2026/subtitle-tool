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
        string entry = Path.Combine(root, "subtitle_app", "subtitle_app.py");
        try
        {
            if (!File.Exists(entry))
                throw new FileNotFoundException("未找到应用入口，请将启动器放在项目根目录中。", entry);

            string python = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
            if (!File.Exists(python))
                python = FindOnPath("pythonw.exe") ?? FindOnPath("pyw.exe");
            if (python == null)
                throw new FileNotFoundException("未找到 Python。请安装 Python 3.10+ 并加入 PATH，或在项目中创建 .venv。");

            Process.Start(new ProcessStartInfo
            {
                FileName = python,
                Arguments = "\"" + entry + "\"",
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

    private static string FindOnPath(string name)
    {
        foreach (string item in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            string directory = Environment.ExpandEnvironmentVariables(item.Trim().Trim('"'));
            if (directory.Length == 0)
                continue;
            string candidate = Path.Combine(directory, name);
            if (File.Exists(candidate))
                return candidate;
        }
        return null;
    }
}
