#pragma once

#include <condition_variable>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

namespace bilingual::notes {

struct Note {
    std::string game_id, game_title, entry_id, source_key, name_space, entry_source;
    std::string kind, target, target_key, en, zh, meaning, meaning_source;
    std::int64_t saved_at_ms = 0;
};

struct Receipt {
    Note note;
    bool inserted = false;
    std::string error;
};

// Only this worker creates the notebook. Desktop readers can start before or after it.
// Every accepted click gets a transaction and a receipt, including duplicates.
class NoteWriter {
public:
    explicit NoteWriter(std::string path);
    ~NoteWriter();
    void submit(Note note);
    std::deque<Receipt> take_receipts();
    void flush();

private:
    void run();
    std::string path_;
    std::mutex mutex_;
    std::condition_variable wake_, drained_;
    std::deque<Note> queue_;
    std::deque<Receipt> receipts_;
    bool closing_ = false, writing_ = false;
    std::thread worker_;
};

} // namespace bilingual::notes
