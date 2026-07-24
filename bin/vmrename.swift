// Rename a Voice Memo by writing THROUGH Core Data, so Core Data emits the persistent-history
// transaction that NSPersistentCloudKitContainer's mirroring delegate exports to iCloud.
//
// A raw SQL UPDATE changes the row but writes no history entry, so the sync engine is never
// told anything happened: the Mac shows the new title, the CKRecord keeps the old one, and a
// later import can overwrite you. Going through Core Data produces a transaction structurally
// identical to a real File>Rename.
//
// SAFETY — the danger here is not the write, it is migration. If the cached model is even
// slightly staler than the store (e.g. after a macOS update), automatic migration will
// silently DROP columns from every recording and rewrite the store's identity hashes. So:
// migration is OFF and there is a hard compatibility gate that aborts instead.
//
// Default merge policy (NSErrorMergePolicy) is kept deliberately: a conflicting concurrent
// write from Voice Memos fails the save loudly rather than clobbering silently.
//
// Build: swiftc -O bin/vmrename.swift -o bin/vmrename
// Usage:
//   vmrename <model.mom> <db> <uniqueID> <newTitle> [--dry-run]      one memo
//   vmrename <model.mom> <db> --batch <file.tsv> [--dry-run]         many; each line "uid\ttitle"
//
// Batch mode saves once per record, so every rename is its own history transaction — one bad
// row fails only itself. Output is one "OK <uid>" / "FAIL <uid> <reason>" line per memo.

import Foundation
import CoreData

let argv = CommandLine.arguments
guard argv.count >= 4 else {
    FileHandle.standardError.write("usage: vmrename <model.mom> <db> (<uid> <title> | --batch <file.tsv>) [--dry-run]\n".data(using: .utf8)!)
    exit(2)
}
let momURL = URL(fileURLWithPath: argv[1])
let dbURL = URL(fileURLWithPath: argv[2])
let dryRun = argv.contains("--dry-run")

func die(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write("ABORT: \(msg)\n".data(using: .utf8)!)
    exit(code)
}

// (uid, title) pairs from either the CLI or a TSV file.
var pairs: [(String, String)] = []
if let bi = argv.firstIndex(of: "--batch") {
    guard argv.count > bi + 1 else { die("--batch needs a file path", 2) }
    guard let text = try? String(contentsOfFile: argv[bi + 1], encoding: .utf8) else {
        die("cannot read batch file", 2)
    }
    for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
        let parts = line.components(separatedBy: "\t")
        if parts.count >= 2 && !parts[0].isEmpty && !parts[1].isEmpty {
            pairs.append((parts[0], parts[1]))
        }
    }
    guard !pairs.isEmpty else { die("batch file had no valid uid\\ttitle lines", 2) }
} else {
    guard argv.count >= 5 else { die("single mode needs <uid> <title>", 2) }
    pairs = [(argv[3], argv[4])]
}

guard let model = NSManagedObjectModel(contentsOf: momURL) else {
    die("could not load model at \(momURL.path)", 3)
}

do {
    let meta = try NSPersistentStoreCoordinator.metadataForPersistentStore(
        ofType: NSSQLiteStoreType, at: dbURL, options: nil)

    // HARD GATE. Never migrate this store; Voice Memos owns its schema. A stale cached model
    // would otherwise silently drop columns from every recording on open.
    guard model.isConfiguration(withName: nil, compatibleWithStoreMetadata: meta) else {
        die("model/store version hashes differ — refusing to open (migration would drop columns)", 4)
    }
    print("gate: model is compatible with store — no migration")

    let psc = NSPersistentStoreCoordinator(managedObjectModel: model)
    let opts: [AnyHashable: Any] = [
        NSPersistentHistoryTrackingKey: true,                          // emit the transaction
        NSMigratePersistentStoresAutomaticallyOption: false,           // never migrate
        NSInferMappingModelAutomaticallyOption: false,                 // never infer
        NSPersistentStoreRemoteChangeNotificationPostOptionKey: true,  // nudge a live voicememod
    ]
    try psc.addPersistentStore(ofType: NSSQLiteStoreType, configurationName: nil,
                               at: dbURL, options: opts)

    let ctx = NSManagedObjectContext(concurrencyType: .mainQueueConcurrencyType)
    ctx.persistentStoreCoordinator = psc
    // Must NOT be prefixed "NSCloudKitMirroringDelegate." — those are the only authors the
    // history analyzer treats as private and skips for export.
    ctx.transactionAuthor = "voice-memo-rename"

    var ok = 0, failed = 0
    for (uid, title) in pairs {
        do {
            let req = NSFetchRequest<NSManagedObject>(entityName: "CloudRecording")
            req.predicate = NSPredicate(format: "uniqueID == %@", uid)  // never match on title
            req.fetchLimit = 2
            let rows = try ctx.fetch(req)
            guard rows.count == 1, let obj = rows.first else {
                print("FAIL \(uid) expected-1-row-got-\(rows.count)"); failed += 1; continue
            }
            if dryRun {
                let old = obj.value(forKey: "encryptedTitle") as? String ?? "(nil)"
                print("DRY \(uid) [\(old)] -> [\(title)]"); ok += 1; continue
            }
            obj.setValue(title, forKey: "encryptedTitle")
            obj.setValue(title, forKey: "customLabelForSorting")
            // customLabel is an ISO-8601 timestamp Voice Memos maintains — leave it alone.
            try ctx.save()                                              // own transaction per memo
            print("OK \(uid)"); ok += 1
        } catch {
            ctx.rollback()
            print("FAIL \(uid) \(error)"); failed += 1
        }
    }
    print("done: \(ok) ok, \(failed) failed\(dryRun ? " (dry-run)" : "")")
    exit(failed == 0 ? 0 : 7)
} catch {
    die("\(error)", 6)
}
