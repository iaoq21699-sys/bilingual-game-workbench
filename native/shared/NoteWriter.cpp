#include "NoteWriter.hpp"
#include <sqlite3.h>
#include <memory>
#include <stdexcept>
#include <utility>

namespace bilingual::notes {
namespace {

void check(sqlite3* db, int rc) {
    if (rc == SQLITE_OK) return;
    switch (rc & 0xff) {
    case SQLITE_BUSY: case SQLITE_LOCKED: throw std::runtime_error("笔记库正被占用");
    case SQLITE_CANTOPEN: case SQLITE_PERM: case SQLITE_READONLY:
        throw std::runtime_error("无法写入笔记库，请检查数据目录权限");
    case SQLITE_FULL: throw std::runtime_error("保存笔记的磁盘空间不足");
    default: throw std::runtime_error(db ? sqlite3_errmsg(db) : sqlite3_errstr(rc));
    }
}

bool insert(const std::string& path, const Note& note) {
    sqlite3* raw = nullptr;
    int rc = sqlite3_open_v2(path.c_str(), &raw, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr);
    std::unique_ptr<sqlite3, decltype(&sqlite3_close_v2)> db(raw, sqlite3_close_v2);
    check(raw, rc);
    check(raw, sqlite3_busy_timeout(raw, 1800));
    check(raw, sqlite3_exec(raw, R"sql(
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY, game_id TEXT NOT NULL, game_title TEXT NOT NULL,
            entry_id TEXT NOT NULL, source_key TEXT NOT NULL, namespace TEXT NOT NULL,
            entry_source TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('sentence','word','phrase')),
            target TEXT NOT NULL, target_key TEXT NOT NULL, en TEXT NOT NULL, zh TEXT NOT NULL,
            meaning TEXT NOT NULL, meaning_source TEXT NOT NULL, saved_at_ms INTEGER NOT NULL,
            personal_note TEXT NOT NULL DEFAULT '', UNIQUE(game_id, entry_id, target_key)
        );
        CREATE INDEX IF NOT EXISTS notes_by_game_time ON notes(game_id, saved_at_ms DESC, id DESC);
    )sql", nullptr, nullptr, nullptr));

    sqlite3_stmt* statement = nullptr;
    rc = sqlite3_prepare_v2(raw, R"sql(
        INSERT INTO notes(game_id,game_title,entry_id,source_key,namespace,entry_source,kind,
                          target,target_key,en,zh,meaning,meaning_source,saved_at_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id,entry_id,target_key) DO NOTHING
    )sql", -1, &statement, nullptr);
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> owned(statement, sqlite3_finalize);
    check(raw, rc);
    int index = 1;
    for (const auto* field : {&note.game_id, &note.game_title, &note.entry_id, &note.source_key,
             &note.name_space, &note.entry_source, &note.kind, &note.target, &note.target_key,
             &note.en, &note.zh, &note.meaning, &note.meaning_source}) {
        check(raw, sqlite3_bind_text(statement, index++, field->c_str(),
                                    static_cast<int>(field->size()), SQLITE_STATIC));
    }
    check(raw, sqlite3_bind_int64(statement, index, note.saved_at_ms));
    rc = sqlite3_step(statement);
    if (rc != SQLITE_DONE) check(raw, rc);
    return sqlite3_changes(raw) == 1; // autocommit has finished
}
} // namespace

NoteWriter::NoteWriter(std::string path) : path_(std::move(path)), worker_([this] { run(); }) {}

NoteWriter::~NoteWriter() {
    { std::lock_guard lock(mutex_); closing_ = true; }
    wake_.notify_one();
    worker_.join();
}

void NoteWriter::submit(Note note) {
    { std::lock_guard lock(mutex_); queue_.push_back(std::move(note)); }
    wake_.notify_one();
}

std::deque<Receipt> NoteWriter::take_receipts() {
    std::deque<Receipt> result;
    std::lock_guard lock(mutex_);
    result.swap(receipts_);
    return result;
}

void NoteWriter::flush() {
    std::unique_lock lock(mutex_);
    drained_.wait(lock, [this] { return queue_.empty() && !writing_; });
}

void NoteWriter::run() {
    for (;;) {
        Receipt receipt;
        {
            std::unique_lock lock(mutex_);
            wake_.wait(lock, [this] { return closing_ || !queue_.empty(); });
            if (queue_.empty()) return;
            receipt.note = std::move(queue_.front());
            queue_.pop_front();
            writing_ = true;
        }
        try { receipt.inserted = insert(path_, receipt.note); }
        catch (const std::exception& e) { receipt.error = e.what(); }
        {
            std::lock_guard lock(mutex_);
            receipts_.push_back(std::move(receipt));
            writing_ = false;
        }
        drained_.notify_all();
    }
}
} // namespace bilingual::notes
