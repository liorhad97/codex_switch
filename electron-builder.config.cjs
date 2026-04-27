const DEFAULT_UPDATE_BASE_URL = "https://pub-1fc6be6e977a4adf8a928d5e615d8f54.r2.dev";
const updateBaseUrl = process.env.CODEX_SWITCH_UPDATE_BASE_URL || DEFAULT_UPDATE_BASE_URL;

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
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true
  },
  electronUpdaterCompatibility: ">=2.16",
  publish: updateBaseUrl
    ? [
        {
          provider: "generic",
          url: updateBaseUrl
        }
      ]
    : undefined
};
