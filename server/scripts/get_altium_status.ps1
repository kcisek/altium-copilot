# Altium Designer 18+ runs as X2.exe; Altium Designer 17 and older run as DXP.exe.
$altium = @(Get-Process -Name "X2", "DXP" -ErrorAction SilentlyContinue)
if ($altium.Count -eq 0) {
    '{"running":false}' | Write-Output
    exit 0
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

public class AltiumStatus {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumWindowsProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);

    // Returns { activeTab, projectFile, projectPath, windowTitle, pid }.
    // pids: every running X2/DXP process. legacyPids: the DXP ones, whose title bar
    // leads with the product name instead of the project; Python parses those.
    public static string[] GetStatus(int[] pids, int[] legacyPids) {
        IntPtr altiumHwnd = IntPtr.Zero;
        IntPtr fallbackHwnd = IntPtr.Zero;

        // The X2 main window is a TDocumentForm. Older DXP builds may use another class,
        // so for DXP fall back to any visible window titled "Altium ...", skipping the VCL
        // TApplication window, which is visible but zero-sized.
        EnumWindows((h, l) => {
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            if (Array.IndexOf(pids, (int)pid) < 0) return true;
            bool dxp = Array.IndexOf(legacyPids, (int)pid) >= 0;
            var cls   = new StringBuilder(256);
            var title = new StringBuilder(512);
            GetClassName(h, cls, 256);
            GetWindowText(h, title, 512);
            if (!title.ToString().Contains("Altium")) return true;
            if (cls.ToString() == "TDocumentForm") {
                altiumHwnd = h;
                return false; // stop — found Altium
            }
            if (dxp && fallbackHwnd == IntPtr.Zero && cls.ToString() != "TApplication" && IsWindowVisible(h))
                fallbackHwnd = h;
            return true;
        }, IntPtr.Zero);

        if (altiumHwnd == IntPtr.Zero)
            altiumHwnd = fallbackHwnd;
        if (altiumHwnd == IntPtr.Zero)
            return new string[] { "NOT_RUNNING", "", "", "", "" };

        var titleSb = new StringBuilder(512);
        GetWindowText(altiumHwnd, titleSb, 512);
        string windowTitle = titleSb.ToString();
        uint altiumPid;
        GetWindowThreadProcessId(altiumHwnd, out altiumPid);
        bool legacy = Array.IndexOf(legacyPids, (int)altiumPid) >= 0;

        string projectFile = "";
        if (!legacy) {
            var m = Regex.Match(windowTitle, @"^(.+?\.PrjPcb)\s*-", RegexOptions.IgnoreCase);
            if (m.Success) projectFile = m.Groups[1].Value.Trim();
        }

        string activeTab = "";
        EnumChildWindows(altiumHwnd, (h, l) => {
            var cls = new StringBuilder(256);
            GetClassName(h, cls, 256);
            if (cls.ToString() == "TdxTabSheet" && IsWindowVisible(h)) {
                var title = new StringBuilder(256);
                GetWindowText(h, title, 256);
                if (title.Length > 0) {
                    activeTab = title.ToString();
                    return false; // stop — first visible tab is the active one
                }
            }
            return true;
        }, IntPtr.Zero);

        // Resolve full project path from known Altium documents root
        string projectPath = "";
        if (projectFile != "") {
            string searchRoot = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.CommonDocuments),
                "Altium"
            );
            if (System.IO.Directory.Exists(searchRoot)) {
                foreach (var f in System.IO.Directory.EnumerateFiles(searchRoot, projectFile,
                             System.IO.SearchOption.AllDirectories)) {
                    projectPath = f;
                    break;
                }
            }
        }

        return new string[] { activeTab, projectFile, projectPath, windowTitle, altiumPid.ToString() };
    }
}
'@

$pids = [int[]]@($altium | ForEach-Object { $_.Id })
$legacyPids = [int[]]@($altium | Where-Object { $_.ProcessName -eq "DXP" } | ForEach-Object { $_.Id })
$r = [AltiumStatus]::GetStatus($pids, $legacyPids)

if ($r[0] -eq "NOT_RUNNING") {
    '{"running":true,"warning":"no_sheet_open","project_file":"","project_path":"","active_tab":""}' | Write-Output
    exit 0
}

$process = $altium | Where-Object { $_.Id -eq [int]$r[4] } | Select-Object -First 1

@{
    running      = $true
    active_tab   = $r[0]
    project_file = $r[1]
    project_path = $r[2]
    window_title = $r[3]
    process      = $process.ProcessName
} | ConvertTo-Json -Compress | Write-Output
