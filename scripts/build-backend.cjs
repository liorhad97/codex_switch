const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const rootDir = path.resolve(__dirname, "..");
const backendName = "codex-switch-backend";
const backendExecutable = process.platform === "win32" ? `${backendName}.exe` : backendName;
const backendDistDir = path.join(rootDir, "dist", "backend");
const backendOutputPath = path.join(backendDistDir, backendExecutable);

function pythonCandidates() {
  if (process.env.PYTHON) {
    return [{ command: process.env.PYTHON, prefixArgs: [] }];
  }

  if (process.platform === "win32") {
    return [
      { command: "py", prefixArgs: ["-3"] },
      { command: "python", prefixArgs: [] },
      { command: "python3", prefixArgs: [] }
    ];
  }

  return [
    { command: "python3.12", prefixArgs: [] },
    { command: "python3.13", prefixArgs: [] },
    { command: "python3", prefixArgs: [] },
    { command: "python", prefixArgs: [] }
  ];
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: rootDir,
    stdio: "inherit",
    ...options
  });

  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }

  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

function hasPyInstaller(candidate) {
  const result = spawnSync(candidate.command, [...candidate.prefixArgs, "-m", "PyInstaller", "--version"], {
    cwd: rootDir,
    encoding: "utf8",
    stdio: "pipe"
  });
  return result.status === 0;
}

const python = pythonCandidates().find(hasPyInstaller);

if (!python) {
  console.error("PyInstaller is required to build the backend executable.");
  console.error("Install it with: python3 -m pip install pyinstaller");
  console.error("Or run with a specific Python: PYTHON=/path/to/python npm run build:backend");
  process.exit(1);
}

fs.rmSync(backendDistDir, { recursive: true, force: true });
fs.mkdirSync(backendDistDir, { recursive: true });

run(python.command, [
  ...python.prefixArgs,
  "-m",
  "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onefile",
  "--name",
  backendName,
  "--distpath",
  path.join("dist", "backend"),
  "--workpath",
  path.join("build", "pyinstaller"),
  "--specpath",
  path.join("build", "pyinstaller"),
  "main.py"
]);

if (!fs.existsSync(backendOutputPath)) {
  console.error(`Expected backend executable was not created: ${backendOutputPath}`);
  process.exit(1);
}

console.log(`Built backend executable: ${backendOutputPath}`);
