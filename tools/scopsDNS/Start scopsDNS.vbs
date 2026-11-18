' Starts the SCOPS window with no console. Windows Terminal ignores -WindowStyle Hidden.
Set sh = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
ps1 = folder & "\SCOPS_UPnP_Check.ps1"
sh.Run "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, False
