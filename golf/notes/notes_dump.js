// Read-only dump of one Apple Notes folder for golf.notes.applenotes (JavaScript for Automation).
//
//   osascript -l JavaScript notes_dump.js meta   "<folder name>"
//   osascript -l JavaScript notes_dump.js bodies "<folder name>" <note id> [<note id> ...]
//
// meta:   every folder with that name in every account, with bulk note metadata (one Apple Event per
//         property array), nested sub-folders included and listed. Bodies are NOT read here.
// bodies: plaintext (never the HTML body, which inlines base64 images) of the given unlocked notes.
// Output is ASCII-only JSON (non-ASCII escaped as \uXXXX) so a launchd/locale encoding can't corrupt it.
// Errors come back as {"ok": false, "error": ..., "errorNumber": ...} (-1743 = Automation denied).

function asciiJSON(obj) {
  return JSON.stringify(obj).replace(/[\u007f-￿]/g, function (c) {
    return "\\u" + ("0000" + c.charCodeAt(0).toString(16)).slice(-4);
  });
}

function iso(d) {
  return d ? d.toISOString() : null;
}

function collectNotes(folder, path, seen, notes, subfolders) {
  const ns = folder.notes;
  const ids = ns.id(), names = ns.name(), created = ns.creationDate(),
        modified = ns.modificationDate(), locked = ns.passwordProtected();
  for (let i = 0; i < ids.length; i++) {
    if (seen[ids[i]]) continue;
    seen[ids[i]] = true;
    notes.push({ id: ids[i], name: names[i], created: iso(created[i]), modified: iso(modified[i]),
                 locked: !!locked[i], folder: path });
  }
  const subs = folder.folders;
  const subNames = subs.name();
  for (let j = 0; j < subNames.length; j++) {
    const subPath = path + "/" + subNames[j];
    const before = notes.length;
    collectNotes(subs[j], subPath, seen, notes, subfolders);
    subfolders.push({ path: subPath, notes: notes.length - before });
  }
}

function meta(Notes, folderName) {
  const accounts = Notes.accounts;
  const accountNames = accounts.name();
  const matches = [], available = [], seenFolders = {};
  for (let a = 0; a < accountNames.length; a++) {
    const folders = accounts[a].folders;
    const names = folders.name(), ids = folders.id();
    for (let k = 0; k < names.length; k++) {
      available.push(accountNames[a] + "/" + names[k]);
      if (names[k] !== folderName || seenFolders[ids[k]]) continue;
      seenFolders[ids[k]] = true;
      const notes = [], subfolders = [];
      collectNotes(folders[k], names[k], {}, notes, subfolders);
      matches.push({ id: ids[k], name: names[k], account: accountNames[a], subfolders: subfolders, notes: notes });
    }
  }
  return { ok: true, accounts: accountNames, folders: matches, available: matches.length ? [] : available };
}

function bodies(Notes, ids) {
  const out = [];
  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    try {
      const note = Notes.notes.byId(id);
      if (note.passwordProtected()) {
        out.push({ id: id, locked: true });
      } else {
        out.push({ id: id, plaintext: note.plaintext() });
      }
    } catch (e) {
      out.push({ id: id, error: String((e && e.message) || e), errorNumber: (e && e.errorNumber) || 0 });
    }
  }
  return { ok: true, bodies: out };
}

function run(argv) {
  const mode = argv[0], folder = argv[1];
  try {
    const Notes = Application("Notes");
    if (mode === "meta") return asciiJSON(meta(Notes, folder));
    if (mode === "bodies") return asciiJSON(bodies(Notes, argv.slice(2)));
    return asciiJSON({ ok: false, error: "unknown mode: " + mode, errorNumber: 0 });
  } catch (e) {
    return asciiJSON({ ok: false, error: String((e && e.message) || e), errorNumber: (e && e.errorNumber) || 0 });
  }
}
