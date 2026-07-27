CREATE TABLE import_receipts_v2 (
    source_hash TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY(source_hash, source_record_id, source_agent)
);

INSERT OR IGNORE INTO import_receipts_v2(
    source_hash, source_record_id, source_agent, imported_at
)
SELECT source_hash, source_record_id, source_agent, imported_at
FROM import_receipts;

DROP TABLE import_receipts;

ALTER TABLE import_receipts_v2 RENAME TO import_receipts;
