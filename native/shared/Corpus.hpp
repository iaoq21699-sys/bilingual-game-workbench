#pragma once

#include <cstdint>
#include <cstddef>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace bilingual::data {

class CorpusError : public std::runtime_error {
public:
    CorpusError(std::string message, int sqlite_code);
    [[nodiscard]] int sqlite_code() const noexcept { return code_; }
private:
    int code_;
};

// UTF-8 throughout. Raw game text and JSON are returned without stripping tags,
// translating, or rewriting formatting parameters. entryPhrases additionally
// exposes the original phrase annotations as typed records using SQLite JSON.
struct Entry {
    std::string id, game_id, key, name_space, category;
    std::string en, zh, speaker, source, note;
    std::string phrases_json, warnings_json;
};

struct SearchRequest {
    std::string query;
    std::string category; // Empty means every category in the selected game.
    int limit = 50;       // Clamped to [1, 500], matching the Python service.
    std::int64_t offset = 0;
};

struct SearchPage {
    std::int64_t total = 0;
    int limit = 50;
    std::int64_t offset = 0;
    bool tokens_truncated = false; // Only the first 12 query tokens are used.
    std::vector<Entry> items;
};

enum class DefinitionKind { Word, Phrase, Contraction };

struct LookupHit {
    std::string term, meaning, source, scope;
    DefinitionKind kind = DefinitionKind::Word;
    // A suffix/irregular-form fallback, NOT a verified lemma or contextual sense.
    bool is_candidate = false;
    bool entry_annotation = false;
};

std::string normalize_term(std::string_view value);

// A read-only connection permanently bound to one game. Never creates a database
// or edits it. Calls on the same instance are serialized; use a worker thread for
// queries so SQLite I/O cannot stall the game's render/input thread.
// Requires sqlite3.h + SQLite at link time. Compile this source as UTF-8 (/utf-8).
class Corpus {
public:
    explicit Corpus(std::string database_path_utf8, std::string game_id,
                    int busy_timeout_ms = 250);
    ~Corpus();
    Corpus(Corpus&&) noexcept;
    Corpus& operator=(Corpus&&) noexcept;
    Corpus(const Corpus&) = delete;
    Corpus& operator=(const Corpus&) = delete;

    [[nodiscard]] const std::string& game_id() const;
    // AND across whitespace-separated literal tokens. Searches the persisted
    // document column (English, Chinese, key, namespace, speaker and notes).
    // '%' / '_' / '\\' are literal characters, never SQL wildcards from input.
    // Count and page are read from the same SQLite snapshot.
    [[nodiscard]] SearchPage search(const SearchRequest& request = {}) const;
    [[nodiscard]] std::optional<Entry> entry(std::string_view entry_id) const;
    [[nodiscard]] std::vector<std::string> categories() const;
    // Exact matches first, current-game scope before global. Never reads another
    // game's definitions. English word-form candidates are optional and labeled.
    // An empty result means no local definition was found.
    [[nodiscard]] std::vector<LookupHit> lookup(std::string_view term,
                                               bool allow_variants = true) const;
    // Bounded reading aids, not sentence-level sense disambiguation. Known
    // presentation tags are skipped; stored text remains unchanged. 128 KiB input
    // limit. Call on entry selection in a worker, never once per render frame.
    // Words: first 2,048 tokens, <=120 distinct English words (case-insensitive).
    // One highest-priority definition per word; term retains sentence spelling.
    [[nodiscard]] std::vector<LookupHit> sentenceWords(std::string_view en,
                                                      std::size_t max_words = 100) const;
    // Phrases: first 256 English tokens, 2-6 adjacent words, <=1,024 candidates.
    // Returns <=100 matching local phrases, longest first, game scope preferred.
    // Entry::phrases_json remains the separate source of explicit entry notes.
    [[nodiscard]] std::vector<LookupHit> sentencePhrases(std::string_view en,
                                                        std::size_t max_phrases = 30) const;
    // Original entry context first, then automatic matches. Terms are deduplicated
    // after English case/whitespace/apostrophe normalization. Keeps original
    // term/meaning/source; does not require literal matching (e.g. "blow it" may
    // annotate "blowing it"). Rejects entries from another game. Raw JSON remains
    // unchanged. Requires SQLite's bundled JSON functions; <=1 MiB / 512 notes.
    [[nodiscard]] std::vector<LookupHit> entryPhrases(const Entry& selected,
                                                     std::size_t max_phrases = 30) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    Impl& impl() const;
};

} // namespace bilingual::data
