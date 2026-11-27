// End-to-end regression of the production capture queue, using a new temporary directory.
#include "NoteWriter.hpp"
#include <filesystem>
#include <iostream>
#include <memory>
#include <sqlite3.h>
#include <stdexcept>

using bilingual::notes::Note;
using bilingual::notes::NoteWriter;

int main(int argc, char** argv) try {
    if (argc != 2) throw std::runtime_error("Pass an empty temporary directory");
    const std::filesystem::path root(argv[1]);
    if (!std::filesystem::is_directory(root) || !std::filesystem::is_empty(root))
        throw std::runtime_error("Test directory must exist and be empty");
    const auto folder = root / "notebook";
    const auto path = (folder / "notes.sqlite3").string();
    Note note;
    note.game_id="test"; note.game_title="Test game"; note.entry_id="first";
    note.en="A blue bird."; note.zh="一只蓝鸟。"; note.kind="word";
    note.target="blue"; note.target_key="blue"; note.meaning="蓝色"; note.meaning_source="原句注释";
    NoteWriter writer(path);
    writer.submit(note);
    writer.flush();
    auto failed = writer.take_receipts();
    if (failed.size()!=1 || failed.front().error.empty() || failed.front().inserted)
        throw std::runtime_error("Failed write was acknowledged as saved");
    std::filesystem::create_directory(folder);
    writer.submit(failed.front().note);
    writer.submit(note);
    writer.flush();
    auto saved = writer.take_receipts();
    if (saved.size()!=2 || !saved[0].inserted || !saved[0].error.empty() ||
        saved[1].inserted || !saved[1].error.empty())
        throw std::runtime_error("Retry or duplicate receipt did not match the commit");
    {
        NoteWriter second(path);
        for (int i=0;i<32;++i) {
            note.entry_id=std::to_string(i);
            note.saved_at_ms=i+1;
            writer.submit(note);
            second.submit(note);
        }
        // No flush: destruction must drain a burst even while the other producer is active.
    }
    writer.flush();
    writer.take_receipts();
    note.entry_id="short"; note.en="Run!"; note.zh="快跑！";
    note.kind="sentence"; note.target="Run!"; note.target_key="";
    note.meaning=""; note.meaning_source="";
    writer.submit(note);
    writer.flush();
    auto whole = writer.take_receipts();
    if (whole.size()!=1 || !whole[0].inserted)
        throw std::runtime_error("Could not seed the previously saved sentence");
    sqlite3* raw = nullptr;
    const int opened = sqlite3_open(path.c_str(), &raw);
    std::unique_ptr<sqlite3, decltype(&sqlite3_close_v2)> db(raw, sqlite3_close_v2);
    if (opened != SQLITE_OK ||
        sqlite3_exec(raw, "UPDATE notes SET target_key='run',personal_note='keep this' WHERE entry_id='short'",
                     nullptr, nullptr, nullptr) != SQLITE_OK)
        throw std::runtime_error("Could not make a legacy sentence fixture");
    writer.submit(note); // Same sentence through another entry point, without its old key.
    note.kind="word"; note.target="Run"; note.target_key="run"; note.meaning="跑开";
    writer.submit(note);
    writer.submit(note);
    writer.flush();
    auto distinct = writer.take_receipts();
    if (distinct.size()!=3 || distinct[0].inserted || !distinct[1].inserted || distinct[2].inserted ||
        !distinct[0].error.empty() || !distinct[1].error.empty() || !distinct[2].error.empty())
        throw std::runtime_error("Short sentence and word were not deduplicated independently");
    sqlite3_stmt* stmt = nullptr;
    int prepared = sqlite3_prepare_v2(raw, R"sql(
        SELECT count(*) FROM notes WHERE entry_id='short' AND (
            (kind='sentence' AND target_key='' AND personal_note='keep this') OR
            (kind='word' AND target_key='run' AND meaning='跑开'))
    )sql", -1, &stmt, nullptr);
    std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> finish(stmt, sqlite3_finalize);
    if (prepared != SQLITE_OK || sqlite3_step(stmt) != SQLITE_ROW || sqlite3_column_int(stmt, 0) != 2)
        throw std::runtime_error("Legacy remark or short-word meaning was lost");
    finish.reset();
    note.entry_id="missing"; note.meaning="本地词典暂无对应释义"; note.meaning_source="未收录";
    note.saved_at_ms=42;
    writer.submit(note);
    writer.flush();
    auto missing = writer.take_receipts();
    if (missing.size()!=1 || !missing[0].inserted || missing[0].completed || !missing[0].error.empty())
        throw std::runtime_error("Could not seed the unlisted word");
    sqlite3_int64 original_id=0;
    if (sqlite3_exec(raw, "UPDATE notes SET personal_note='keep this too' WHERE entry_id='missing'",
                     nullptr, nullptr, nullptr) != SQLITE_OK ||
        sqlite3_exec(raw, "SELECT id FROM notes WHERE entry_id='missing'",
                     [](void* out,int,char** values,char**) {
                         *static_cast<sqlite3_int64*>(out)=std::stoll(values[0]); return 0;
                     }, &original_id, nullptr) != SQLITE_OK || !original_id)
        throw std::runtime_error("Could not set the original word remark");
    writer.submit(note);
    note.meaning="离开"; note.meaning_source="离线词典"; note.saved_at_ms=100;
    writer.submit(note);
    note.meaning="不要覆盖";
    writer.submit(note);
    writer.flush();
    auto complete = writer.take_receipts();
    if (complete.size()!=3 || complete[0].completed || !complete[1].completed || complete[2].completed)
        throw std::runtime_error("Unexpected completion receipts");
    for (const auto& receipt : complete) {
        if (receipt.inserted || !receipt.error.empty())
            throw std::runtime_error("Completion created a duplicate or returned an error");
    }
    prepared = sqlite3_prepare_v2(raw, R"sql(
        SELECT id FROM notes WHERE entry_id='missing' AND meaning='离开' AND meaning_source='离线词典'
            AND personal_note='keep this too' AND saved_at_ms=42
    )sql", -1, &stmt, nullptr);
    finish.reset(stmt);
    if (prepared != SQLITE_OK || sqlite3_step(stmt) != SQLITE_ROW ||
        sqlite3_column_int64(stmt,0) != original_id || sqlite3_step(stmt) != SQLITE_DONE)
        throw std::runtime_error("Existing vocabulary card did not retain identity, age, or remark");
    std::cout << "Write failure, retry, duplicate receipt, concurrent shutdown, legacy short-line collision, and vocabulary completion exercised.\n";
    return 0;
} catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
}
