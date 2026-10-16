<#
.SYNOPSIS
    Inject keystrokes into Altium Designer to generate a Protel netlist.
    Design > Netlist > Protel  (Alt+D, N, R, P, Enter)

.PARAMETER Delay
    Milliseconds to wait between keystrokes. Default 80.
#>

param(
    [int]$Delay = 80
)

Add-Type -AssemblyName System.Windows.Forms

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public class AltiumNetlistGenerator {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    public static bool IsAltiumForeground() {
        return _hwnd != IntPtr.Zero && GetForegroundWindow() == _hwnd;
    }

    private static IntPtr _hwnd = IntPtr.Zero;

    // pids: the running X2/DXP processes. Only their windows are candidates, so a browser
    // tab titled "Altium Designer ..." never receives the keystrokes. The TDocumentForm
    // main window is preferred; older DXP builds fall back to any visible window titled
    // "Altium Designer" except the VCL TApplication window.
    public static bool FindAndActivate(int[] pids) {
        _hwnd = IntPtr.Zero;
        IntPtr fallback = IntPtr.Zero;
        EnumWindows((hwnd, lp) => {
            uint pid;
            GetWindowThreadProcessId(hwnd, out pid);
            if (Array.IndexOf(pids, (int)pid) < 0 || !IsWindowVisible(hwnd)) return true;
            var sb = new StringBuilder(512);
            GetWindowText(hwnd, sb, 512);
            if (!sb.ToString().Contains("Altium Designer")) return true;
            var cls = new StringBuilder(256);
            GetClassName(hwnd, cls, 256);
            if (cls.ToString() == "TDocumentForm") {
                _hwnd = hwnd;
                return false;
            }
            if (fallback == IntPtr.Zero && cls.ToString() != "TApplication")
                fallback = hwnd;
            return true;
        }, IntPtr.Zero);

        if (_hwnd == IntPtr.Zero) _hwnd = fallback;
        if (_hwnd == IntPtr.Zero) return false;

        if (IsIconic(_hwnd))
            ShowWindow(_hwnd, 9); // SW_RESTORE
        else
            ShowWindow(_hwnd, 5); // SW_SHOW

        // AttachThreadInput trick: share the input queue so SetForegroundWindow
        // actually works even when this process doesn't own the foreground.
        uint altiumPid;
        uint altiumThread = GetWindowThreadProcessId(_hwnd, out altiumPid);
        uint currentThread = GetCurrentThreadId();
        bool attached = altiumThread != currentThread &&
                        AttachThreadInput(currentThread, altiumThread, true);
        SetForegroundWindow(_hwnd);
        BringWindowToTop(_hwnd);
        if (attached)
            AttachThreadInput(currentThread, altiumThread, false);

        return true;
    }
}
'@

# Altium Designer 18+ runs as X2.exe; Altium Designer 17 and older run as DXP.exe.
$altiumPids = [int[]]@(Get-Process -Name "X2", "DXP" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
if (-not [AltiumNetlistGenerator]::FindAndActivate($altiumPids)) {
    @{ success = $false; error = "Altium Designer window not found. Is it running?" } |
        ConvertTo-Json -Compress | Write-Output
    exit 1
}

# Wait for Altium to actually reach the foreground
$focused = $false
for ($i = 0; $i -lt 50; $i++) {
    Start-Sleep -Milliseconds 100
    if ([AltiumNetlistGenerator]::IsAltiumForeground()) { $focused = $true; break }
}
if (-not $focused) {
    @{ success = $false; error = "Could not bring Altium Designer to the foreground. Click on Altium and try again." } |
        ConvertTo-Json -Compress | Write-Output
    exit 1
}

# Settle time after confirmed focus — give Altium time to finish any in-progress rendering.
Start-Sleep -Milliseconds 2000

function Send-KeyIfFocused($key) {
    if (-not [AltiumNetlistGenerator]::IsAltiumForeground()) {
        @{ success = $false; error = "Altium lost focus mid-sequence. Another window stole focus." } |
            ConvertTo-Json -Compress | Write-Output
        exit 1
    }
    [System.Windows.Forms.SendKeys]::SendWait($key)
    Start-Sleep -Milliseconds $Delay
}

# ESC first to clear any residual menu state from a previous attempt
Send-KeyIfFocused "{ESC}"
Start-Sleep -Milliseconds 200

# Design > Netlist > Protel > Enter, then ESC to dismiss any lingering dialog
Send-KeyIfFocused "%d"
Start-Sleep -Milliseconds 500  # wait for Design menu to open before navigating
Send-KeyIfFocused "n"
Send-KeyIfFocused "r"
Send-KeyIfFocused "p"
Send-KeyIfFocused "{ENTER}"
Start-Sleep -Milliseconds 1000
Send-KeyIfFocused "{ESC}"

@{ success = $true } | ConvertTo-Json -Compress | Write-Output
