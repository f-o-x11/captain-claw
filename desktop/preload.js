/**
 * Captain Claw – Electron preload script.
 *
 * Exposes a minimal bridge to the renderer so the web UI can detect
 * that it is running inside the desktop shell.
 */

const { contextBridge } = require("electron");

// Preloads run sandboxed (Electron's default): require() reaches only a few
// built-in modules, so a require("./package.json") here threw and the page
// never got the bridge. main.js passes the version as an argument instead.
const versionArg = process.argv.find((arg) => arg.startsWith("--captain-claw-version="));

contextBridge.exposeInMainWorld("captainClawDesktop", {
  platform: process.platform,
  arch: process.arch,
  isDesktop: true,
  version: versionArg ? versionArg.split("=")[1] : undefined,
});
