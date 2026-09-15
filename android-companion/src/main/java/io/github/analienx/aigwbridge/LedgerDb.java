package io.github.analienx.aigwbridge;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;

final class LedgerDb extends SQLiteOpenHelper {
    static final class Record {
        final String fingerprint;
        final String delegationId;
        final String task;
        final String status;
        final String resultJson;
        final long updatedAt;

        Record(
                String fingerprint,
                String delegationId,
                String task,
                String status,
                String resultJson,
                long updatedAt
        ) {
            this.fingerprint = fingerprint;
            this.delegationId = delegationId;
            this.task = task;
            this.status = status;
            this.resultJson = resultJson;
            this.updatedAt = updatedAt;
        }
    }

    LedgerDb(Context context) {
        super(context, "relay-ledger.sqlite3", null, 1);
        setWriteAheadLoggingEnabled(true);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL(
                "CREATE TABLE relay ("
                        + "fingerprint TEXT PRIMARY KEY, "
                        + "delegation_id TEXT NOT NULL, "
                        + "task TEXT NOT NULL, "
                        + "status TEXT NOT NULL, "
                        + "result_json TEXT, "
                        + "updated_at INTEGER NOT NULL)"
        );
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        throw new IllegalStateException("No ledger migration is defined yet.");
    }

    synchronized Record claim(String fingerprint, String delegationId, String task) {
        SQLiteDatabase db = getWritableDatabase();
        ContentValues values = new ContentValues();
        values.put("fingerprint", fingerprint);
        values.put("delegation_id", delegationId);
        values.put("task", task);
        values.put("status", "pending");
        values.put("updated_at", System.currentTimeMillis());
        db.insertWithOnConflict("relay", null, values, SQLiteDatabase.CONFLICT_IGNORE);
        return get(fingerprint);
    }

    synchronized Record get(String fingerprint) {
        try (Cursor cursor = getReadableDatabase().query(
                "relay",
                new String[]{
                        "fingerprint",
                        "delegation_id",
                        "task",
                        "status",
                        "result_json",
                        "updated_at"
                },
                "fingerprint = ?",
                new String[]{fingerprint},
                null,
                null,
                null
        )) {
            if (!cursor.moveToFirst()) {
                return null;
            }
            return new Record(
                    cursor.getString(0),
                    cursor.getString(1),
                    cursor.getString(2),
                    cursor.getString(3),
                    cursor.isNull(4) ? null : cursor.getString(4),
                    cursor.getLong(5)
            );
        }
    }

    synchronized List<Record> listOutstanding(int limit) {
        List<Record> records = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().query(
                "relay",
                new String[]{
                        "fingerprint",
                        "delegation_id",
                        "task",
                        "status",
                        "result_json",
                        "updated_at"
                },
                "status IN (?, ?)",
                new String[]{"pending", "result_ready"},
                null,
                null,
                "updated_at ASC",
                Integer.toString(Math.max(1, limit))
        )) {
            while (cursor.moveToNext()) {
                records.add(new Record(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getString(3),
                        cursor.isNull(4) ? null : cursor.getString(4),
                        cursor.getLong(5)
                ));
            }
        }
        return records;
    }

    synchronized void update(String fingerprint, String status, String resultJson) {
        ContentValues values = new ContentValues();
        values.put("status", status);
        if (resultJson != null) {
            values.put("result_json", resultJson);
        }
        values.put("updated_at", System.currentTimeMillis());
        getWritableDatabase().update(
                "relay",
                values,
                "fingerprint = ?",
                new String[]{fingerprint}
        );
    }

    synchronized void touchPending(String fingerprint) {
        ContentValues values = new ContentValues();
        values.put("status", "pending");
        values.put("updated_at", System.currentTimeMillis());
        getWritableDatabase().update(
                "relay",
                values,
                "fingerprint = ?",
                new String[]{fingerprint}
        );
    }

    synchronized void clearAll() {
        getWritableDatabase().delete("relay", null, null);
    }
}
