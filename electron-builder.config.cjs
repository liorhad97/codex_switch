module.exports = {
  appId: "com.codexswitch.desktop",
  productName: "codex switch",
  artifactName: "${productName}-${version}-${os}-${arch}.${ext}",
  directories: {
    output: "release"
  },
  files: [
    "electron/**/*",
    "package.json"
  ],
  extraResources: [
    {
      from: "web/dist",
      to: "web/dist"
    },
    {
      from: "dist/backend",
      to: "backend"
    }
  ],
  asar: true,
  mac: {
    icon: "electron/assets/codex-switch-icon.icns",
    forceCodeSigning: false,
    target: [
      "dmg",
      "zip"
    ],
    category: "public.app-category.productivity"
  },
  win: {
    icon: "electron/assets/codex-switch-icon.ico",
    target: [
      "nsis",
      "zip"
    ]
  },
  linux: {
    icon: "electron/assets/codex-switch-icon.png",
    target: [
      "AppImage",
      "zip"
    ],
    category: "Utility"
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true
  }
};
