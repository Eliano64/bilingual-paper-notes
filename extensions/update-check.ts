/**
 * Notice when the installed copy of this package is behind its remote.
 *
 * pi reconciles a git package only when `pi update --extensions` runs, so an
 * update otherwise sits there unnoticed. On every session start this compares
 * the local revision with the remote one and, when they differ, prints a notice
 * and a footer status. The comparison costs one `git ls-remote` at most every
 * six hours; in between the cached answer is reused, and once the copy has been
 * updated the check goes quiet by itself.
 *
 * It only reads: nothing is fetched into the clone and no checkout is touched.
 * Set BPN_NO_UPDATE_CHECK to skip it entirely.
 */
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const STATE_FILE = join(PACKAGE_ROOT, ".git", "bilingual-paper-notes-update-check.json");
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
const GIT_TIMEOUT_MS = 8000;
const STATUS_KEY = "bilingual-paper-notes";
const NO_CHECK_ENV = "BPN_NO_UPDATE_CHECK";

/** Run git in the package clone; null when git is absent, slow, or unhappy. */
function git(args) {
  return new Promise((done) => {
    const child = spawn("git", ["-C", PACKAGE_ROOT, ...args], {
      timeout: GIT_TIMEOUT_MS,
      windowsHide: true,
    });
    let stdout = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.on("error", () => done(null));
    child.on("close", (code) => done(code === 0 ? stdout.trim() : null));
  });
}

/** The last answer, or null when there is none to trust. */
function readState() {
  try {
    const state = JSON.parse(readFileSync(STATE_FILE, "utf8"));
    if (typeof state.checkedAt !== "number" || typeof state.remote !== "string") return null;
    return state;
  } catch {
    return null;
  }
}

function writeState(state) {
  try {
    writeFileSync(STATE_FILE, JSON.stringify(state));
  } catch {
    // A cache that cannot be written only costs one more lookup next session.
  }
}

/** The remote revision. This is the one network call, and only when the cache is stale. */
async function remoteRevision() {
  const answer = await git(["ls-remote", "origin", "HEAD"]);
  return answer ? answer.split(/\s+/)[0] : null;
}

export default function (pi) {
  pi.on("session_start", async (_event, ctx) => {
    if (process.env[NO_CHECK_ENV]) return;
    try {
      const local = await git(["rev-parse", "HEAD"]);
      if (!local) return; // an npm install is not a checkout, so there is nothing to compare

      const cached = readState();
      let remote = cached ? cached.remote : null;
      if (!cached || Date.now() - cached.checkedAt > CHECK_INTERVAL_MS) {
        remote = await remoteRevision();
        if (remote) writeState({ checkedAt: Date.now(), remote });
      }
      if (!remote || remote === local) return;

      if (ctx.hasUI) {
        ctx.ui.notify(
          "bilingual-paper-notes: the remote has a newer revision — run `pi update --extensions`",
          "info",
        );
        ctx.ui.setStatus(STATUS_KEY, "update available");
      }
    } catch {
      // An update check must never disturb a session.
    }
  });
}
