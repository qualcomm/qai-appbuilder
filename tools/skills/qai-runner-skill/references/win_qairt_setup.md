# Task: Setup QAIRT Environment on Windows

You are allowed to run system commands on the host machine.

Steps to perform:

1. 
1.1. Ask the user for input:
   - RootDirPath (default: C:\WoS_AI)
   
1.2. get setup script from skill resource -scripts.
    - ScriptPath  (scripts\qnn_setup.ps1)
2. Launch a NEW Windows PowerShell window with Administrator permission by running:
   Start-Process powershell.exe -Verb RunAs -ArgumentList @(
       '-NoExit',
       '-ExecutionPolicy','Bypass',
       '-Command', "& {
           Set-Location ''{RootDirPath}'';
           . ''{ScriptPath}'';
           QNN_Setup -rootDirPath ''{RootDirPath}''
       }"
   )

3. Replace {RootDirPath} and {ScriptPath} with the values provided by the user.

4. Do not close the new window automatically. The installation may take a long time.

5. After launching the window, report success and stop.

6. Create Convenience script to source after setup.

wait user to confirm install ready. 

it will install qairt sdk. and python venv. there are setup script.s
qairt\<SDK_VERSION>\bin\envsetup.ps1
<PYTHON_VENV>\script\activate.ps1

Create a helper script `aipcenv.ps1` to initialize environment a. qairt envsetup.ps1 b. python evn. use absolute path of the setup script.

Run it with:
```powershell
. aipcenv.ps1
```