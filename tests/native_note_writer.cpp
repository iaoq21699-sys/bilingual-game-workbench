// End-to-end regression of the production capture queue, using a new temporary directory.
#include "NoteWriter.hpp"
#include <filesystem>
#include <iostream>
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
    std::cout << "Write failure, retry, duplicate receipt and concurrent shutdown exercised.\n";
    return 0;
} catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
}
