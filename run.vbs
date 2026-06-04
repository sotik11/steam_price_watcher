Dim fso, dir, python, script
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
python = dir & "\.venv\Scripts\python.exe"
script = dir & "\watch.py"

Dim wsh
Set wsh = CreateObject("WScript.Shell")
wsh.Run """" & python & """ """ & script & """", 0, False
