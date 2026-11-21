#include "Corpus.hpp"
#include <sqlite3.h>

#include <algorithm>
#include <climits>
#include <mutex>
#include <unordered_set>
#include <utility>

namespace bilingual::data {
namespace {

constexpr const char* kEntryColumns =
    "id,game_id,key,namespace,category,en,zh,speaker,source,note,phrases,warnings";

[[noreturn]] void fail(sqlite3* db, std::string_view operation, int code) {
    throw CorpusError(std::string(operation) + ": " +
        (db ? sqlite3_errmsg(db) : sqlite3_errstr(code)), code);
}

void check(sqlite3* db, int code, std::string_view operation) {
    if (code != SQLITE_OK) fail(db, operation, code);
}

struct CloseDatabase {
    void operator()(sqlite3* db) const noexcept { if (db) sqlite3_close_v2(db); }
};
using Connection = std::unique_ptr<sqlite3, CloseDatabase>;

class Statement {
public:
    Statement(sqlite3* db, const std::string& sql) : db_(db) {
        sqlite3_stmt* raw = nullptr;
        const int code = sqlite3_prepare_v2(db, sql.c_str(), -1, &raw, nullptr);
        statement_.reset(raw);
        check(db, code, "prepare query");
    }
    void bind(int index, std::string_view value) {
        if (value.size() > INT_MAX) throw std::invalid_argument("SQLite parameter is too long");
        check(db_, sqlite3_bind_text(statement_.get(), index,
            value.empty() ? "" : value.data(), static_cast<int>(value.size()),
            SQLITE_TRANSIENT), "bind text");
    }
    void bind(int index, std::int64_t value) {
        check(db_, sqlite3_bind_int64(statement_.get(), index, value), "bind integer");
    }
    bool row() {
        const int code = sqlite3_step(statement_.get());
        if (code == SQLITE_ROW) return true;
        if (code == SQLITE_DONE) return false;
        fail(db_, "read query", code);
    }
    std::string text(int column) const {
        if (sqlite3_column_type(statement_.get(), column) == SQLITE_NULL) return {};
        const auto* value = sqlite3_column_text(statement_.get(), column);
        if (!value) fail(db_, "read UTF-8 text", SQLITE_NOMEM);
        const int size = sqlite3_column_bytes(statement_.get(), column);
        return {reinterpret_cast<const char*>(value), static_cast<std::size_t>(size)};
    }
    std::int64_t integer(int column) const {
        return sqlite3_column_int64(statement_.get(), column);
    }
private:
    struct Finalize {
        void operator()(sqlite3_stmt* value) const noexcept { if (value) sqlite3_finalize(value); }
    };
    sqlite3* db_;
    std::unique_ptr<sqlite3_stmt, Finalize> statement_;
};

class ReadSnapshot {
public:
    explicit ReadSnapshot(sqlite3* db) : db_(db) {
        check(db_, sqlite3_exec(db_, "BEGIN", nullptr, nullptr, nullptr), "begin read snapshot");
    }
    ~ReadSnapshot() { if (active_) sqlite3_exec(db_, "ROLLBACK", nullptr, nullptr, nullptr); }
    void commit() {
        check(db_, sqlite3_exec(db_, "COMMIT", nullptr, nullptr, nullptr), "finish read snapshot");
        active_ = false;
    }
    ReadSnapshot(const ReadSnapshot&) = delete;
    ReadSnapshot& operator=(const ReadSnapshot&) = delete;
private:
    sqlite3* db_;
    bool active_ = true;
};

// Decode just enough Unicode to validate UTF-8 and recognize Python-style
// whitespace. No byte-wise case conversion is performed on non-ASCII text.
std::uint32_t next_codepoint(std::string_view value, std::size_t& position) {
    const auto byte = static_cast<unsigned char>(value[position++]);
    if (byte < 0x80) return byte;
    int extra;
    std::uint32_t code, minimum;
    if (byte >= 0xC2 && byte <= 0xDF) { extra = 1; code = byte & 0x1F; minimum = 0x80; }
    else if (byte >= 0xE0 && byte <= 0xEF) { extra = 2; code = byte & 0x0F; minimum = 0x800; }
    else if (byte >= 0xF0 && byte <= 0xF4) { extra = 3; code = byte & 0x07; minimum = 0x10000; }
    else throw std::invalid_argument("Input is not valid UTF-8");
    if (value.size() - position < static_cast<std::size_t>(extra))
        throw std::invalid_argument("Truncated UTF-8 input");
    for (int i = 0; i < extra; ++i) {
        const auto continuation = static_cast<unsigned char>(value[position++]);
        if ((continuation & 0xC0) != 0x80) throw std::invalid_argument("Invalid UTF-8 continuation");
        code = (code << 6) | (continuation & 0x3F);
    }
    if (code < minimum || code > 0x10FFFF || (code >= 0xD800 && code <= 0xDFFF))
        throw std::invalid_argument("Invalid UTF-8 code point");
    return code;
}

bool whitespace(std::uint32_t code) {
    return (code >= 9 && code <= 13) || (code >= 0x1C && code <= 0x20) ||
        code == 0x85 || code == 0xA0 || code == 0x1680 ||
        (code >= 0x2000 && code <= 0x200A) || code == 0x2028 ||
        code == 0x2029 || code == 0x202F || code == 0x205F || code == 0x3000;
}

void validate(std::string_view value, std::size_t maximum, const char* name) {
    if (value.size() > maximum || value.find('\0') != std::string_view::npos)
        throw std::invalid_argument(std::string(name) + " is too long or contains NUL");
    for (std::size_t i = 0; i < value.size();) next_codepoint(value, i);
}

std::vector<std::string> tokens(std::string_view value) {
    std::vector<std::string> result;
    std::size_t start = std::string_view::npos;
    for (std::size_t position = 0; position < value.size();) {
        const auto before = position;
        if (whitespace(next_codepoint(value, position))) {
            if (start != std::string_view::npos) {
                result.emplace_back(value.substr(start, before - start));
                start = std::string_view::npos;
            }
        } else if (start == std::string_view::npos) start = before;
    }
    if (start != std::string_view::npos) result.emplace_back(value.substr(start));
    return result;
}

std::string normalize(std::string_view value) {
    std::string result;
    for (const auto& token : tokens(value)) {
        if (!result.empty()) result += ' ';
        for (std::size_t i = 0; i < token.size(); ++i) {
            if (token.compare(i, 3, "\xE2\x80\x99") == 0) { result += '\''; i += 2; }
            else {
                const auto ch = static_cast<unsigned char>(token[i]);
                result += static_cast<char>(ch >= 'A' && ch <= 'Z' ? ch + ('a' - 'A') : ch);
            }
        }
    }
    return result;
}

std::string literal_pattern(std::string_view token) {
    std::string result = "%";
    for (char ch : token) {
        if (ch == '%' || ch == '_' || ch == '\\') result += '\\';
        result += ch;
    }
    result += '%';
    return result;
}

using Mapping = std::pair<std::string_view, std::string_view>;
constexpr Mapping kIrregular[] = {
    {"was","be"},{"were","be"},{"is","be"},{"are","be"},{"am","be"},{"been","be"},
    {"went","go"},{"gone","go"},{"did","do"},{"done","do"},{"had","have"},{"has","have"},
    {"saw","see"},{"seen","see"},{"took","take"},{"taken","take"},{"made","make"},
    {"thought","think"},{"knew","know"},{"known","know"},{"felt","feel"},{"found","find"},
    {"got","get"},{"gave","give"},{"given","give"},{"said","say"},{"told","tell"},
    {"could","can"},{"would","will"},{"should","shall"},{"better","good"},{"best","good"},
    {"children","child"},{"people","person"},{"men","man"},{"women","woman"},
    {"feet","foot"},{"teeth","tooth"},{"left","leave"},{"kept","keep"},
    {"bought","buy"},{"brought","bring"}
};
constexpr Mapping kContractions[] = {
    {"don't","do not"},{"doesn't","does not"},{"didn't","did not"},{"can't","cannot"},
    {"won't","will not"},{"isn't","is not"},{"aren't","are not"},{"wasn't","was not"},
    {"weren't","were not"},{"i'm","I am"},{"you're","you are"},{"we're","we are"},
    {"they're","they are"},{"it's","it is / it has"},{"that's","that is / that has"},
    {"i've","I have"},{"we've","we have"},{"you've","you have"},{"i'll","I will"},
    {"let's","let us"},{"haven't","have not"},{"hasn't","has not"},
    {"couldn't","could not"},{"wouldn't","would not"},{"shouldn't","should not"},
    {"i'd","I would / I had"}
};

std::vector<std::string> variants(const std::string& word) {
    std::vector<std::string> result;
    if (word.empty() || !std::all_of(word.begin(), word.end(), [](unsigned char c) {
            return (c >= 'a' && c <= 'z') || c == '\'' || c == '-';
        })) return result; // Never reduce multiword phrases or Chinese terms.
    const auto add = [&](std::string candidate) {
        if (!candidate.empty() && candidate != word &&
            std::find(result.begin(), result.end(), candidate) == result.end())
            result.push_back(std::move(candidate));
    };
    for (const auto& [form, lemma] : kIrregular) if (word == form) add(std::string(lemma));
    if (word.ends_with("'s")) add(word.substr(0, word.size() - 2));
    if (word.ends_with("ies") && word.size() > 4) add(word.substr(0, word.size() - 3) + "y");
    for (std::string_view suffix : {"ing", "ed", "es", "s"}) {
        if (!word.ends_with(suffix) || word.size() <= suffix.size() + 2) continue;
        const auto stem = word.substr(0, word.size() - suffix.size());
        add(stem); add(stem + "e");
        if (stem.size() > 2 && stem.back() == stem[stem.size() - 2]) add(stem.substr(0, stem.size() - 1));
    }
    return result;
}

Entry read_entry(const Statement& row) {
    return {row.text(0), row.text(1), row.text(2), row.text(3), row.text(4),
        row.text(5), row.text(6), row.text(7), row.text(8), row.text(9),
        row.text(10), row.text(11)};
}

std::vector<LookupHit> definitions(sqlite3* db, const std::string& game,
                                   const std::string& term, bool include_phrases) {
    const std::string sql = include_phrases ?
        "SELECT term,meaning,source,scope,kind FROM ("
        "SELECT term,meaning,source,scope,0 AS kind FROM lexicon WHERE term=?1 AND scope IN (?2,'global') "
        "UNION ALL SELECT term,meaning,source,scope,1 AS kind FROM phrases WHERE term=?1 AND scope IN (?2,'global')) "
        "ORDER BY CASE WHEN scope=?2 THEN 0 ELSE 1 END,kind" :
        "SELECT term,meaning,source,scope,0 FROM lexicon WHERE term=?1 AND scope IN (?2,'global') "
        "ORDER BY CASE WHEN scope=?2 THEN 0 ELSE 1 END";
    Statement query(db, sql);
    query.bind(1, term); query.bind(2, game);
    std::vector<LookupHit> result;
    while (query.row()) result.push_back({query.text(0), query.text(1), query.text(2),
        query.text(3), query.integer(4) == 1 ? DefinitionKind::Phrase : DefinitionKind::Word, false});
    return result;
}

std::vector<LookupHit> lookup_definitions(sqlite3* db, const std::string& game,
                                         const std::string& normalized, bool allow_variants) {
    auto result = definitions(db, game, normalized, true);
    if (result.empty() && allow_variants) {
        for (const auto& candidate : variants(normalized)) {
            result = definitions(db, game, candidate, false);
            if (result.empty()) continue;
            for (auto& hit : result) {
                hit.is_candidate = true;
                hit.source += "；词形候选 " + normalized + " → " + candidate + "，需结合句意";
            }
            break;
        }
    }
    if (result.empty()) {
        for (const auto& [form, expanded] : kContractions) {
            if (normalized == form) {
                result.push_back({normalized, "缩写展开：" + std::string(expanded),
                    "常见缩写；多义展开需结合上下文", "builtin", DefinitionKind::Contraction, false});
                break;
            }
        }
    }
    return result;
}

std::string sentence_text(std::string_view value) {
    std::string result;
    result.reserve(value.size());
    for (std::size_t i = 0; i < value.size();) {
        if (value[i] == '<' || value[i] == '{') {
            const auto end = value.find(value[i] == '<' ? '>' : '}', i + 1);
            if (end != std::string_view::npos) { result += ' '; i = end + 1; continue; }
        }
        if (value[i] == '[') {
            const auto end = value.find(']', i + 1);
            if (end != std::string_view::npos && end - i <= 8) {
                const auto tag = normalize(value.substr(i, end - i + 1));
                if (tag == "[blue]" || tag == "[/blue]" || tag == "[gold]" || tag == "[/gold]") {
                    result += ' '; i = end + 1; continue;
                }
            }
        }
        if (value[i] == '&') {
            const auto end = value.find(';', i + 1);
            if (end != std::string_view::npos && end - i <= 16) {
                const auto entity = value.substr(i + 1, end - i - 1);
                if (entity == "apos" || entity == "#39" || entity == "#x27" || entity == "rsquo") result += '\'';
                else if (entity == "quot") result += '"';
                else if (entity == "amp") result += '&';
                else if (entity == "lt") result += '<';
                else if (entity == "gt") result += '>';
                else if (entity == "nbsp" || entity == "#160") result += ' ';
                else result += '|'; // Unknown entities are boundaries, not vocabulary.
                i = end + 1; continue;
            }
        }
        if (value.substr(i, 3) == "\xE2\x80\x99") { result += '\''; i += 3; }
        else result += value[i++];
    }
    return result;
}

struct SentenceToken { std::string word; std::size_t begin, end; };
bool english_letter(char ch) { return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z'); }

std::vector<SentenceToken> sentence_tokens(const std::string& text, std::size_t maximum) {
    std::vector<SentenceToken> result;
    for (std::size_t i = 0; i < text.size() && result.size() < maximum;) {
        if (!english_letter(text[i])) { ++i; continue; }
        const auto begin = i++;
        while (i < text.size()) {
            if (english_letter(text[i])) { ++i; continue; }
            if ((text[i] == '\'' || text[i] == '-') && i + 1 < text.size() && english_letter(text[i + 1])) {
                i += 2; continue;
            }
            break;
        }
        result.push_back({text.substr(begin, i - begin), begin, i});
    }
    return result;
}

bool only_whitespace(std::string_view value) {
    for (std::size_t i = 0; i < value.size();) if (!whitespace(next_codepoint(value, i))) return false;
    return true;
}

std::vector<LookupHit> automatic_phrases(sqlite3* db, const std::string& game,
                                         std::string_view en, std::size_t max_phrases) {
    const auto text = sentence_text(en);
    const auto words = sentence_tokens(text, 256);
    std::unordered_set<std::string> seen_candidates;
    std::vector<std::string> candidates;
    for (std::size_t start = 0; start < words.size() && candidates.size() < 1024; ++start) {
        auto candidate = normalize(words[start].word);
        for (std::size_t end = start + 1; end < words.size() && end < start + 6; ++end) {
            if (!only_whitespace(std::string_view(text).substr(words[end - 1].end, words[end].begin - words[end - 1].end))) break;
            candidate += ' '; candidate += normalize(words[end].word);
            if (candidate.size() > 192) break;
            if (seen_candidates.insert(candidate).second) candidates.push_back(candidate);
            if (candidates.size() == 1024) break;
        }
    }
    if (candidates.empty()) return {};
    std::vector<LookupHit> matches;
    for (std::size_t start = 0; start < candidates.size(); start += 400) {
        const auto end = std::min(start + 400, candidates.size());
        std::string sql = "SELECT term,meaning,source,scope FROM phrases WHERE scope IN (?1,'global') AND term IN (";
        for (std::size_t i = start; i < end; ++i) { if (i > start) sql += ','; sql += '?'; }
        sql += ')';
        Statement query(db, sql);
        query.bind(1, game);
        int index = 2;
        for (std::size_t i = start; i < end; ++i) query.bind(index++, candidates[i]);
        while (query.row()) matches.push_back({query.text(0), query.text(1), query.text(2), query.text(3), DefinitionKind::Phrase, false});
    }
    std::sort(matches.begin(), matches.end(), [&](const LookupHit& left, const LookupHit& right) {
        if (left.term.size() != right.term.size()) return left.term.size() > right.term.size();
        if (left.term != right.term) return left.term < right.term;
        return left.scope == game && right.scope != game;
    });
    std::unordered_set<std::string> seen;
    std::vector<LookupHit> result;
    for (auto& match : matches) {
        if (!seen.insert(match.term).second) continue;
        result.push_back(std::move(match));
        if (result.size() == max_phrases) break;
    }
    return result;
}

} // namespace

std::string normalize_term(std::string_view value) { return normalize(value); }

struct Corpus::Impl {
    Connection connection;
    std::string game;
    std::mutex mutex;
};

CorpusError::CorpusError(std::string message, int sqlite_code)
    : std::runtime_error(std::move(message)), code_(sqlite_code) {}

Corpus::Corpus(std::string database_path_utf8, std::string game_id, int busy_timeout_ms)
    : impl_(std::make_unique<Impl>()) {
    validate(database_path_utf8, 32768, "Database path");
    validate(game_id, 4096, "Game id");
    if (database_path_utf8.empty() || database_path_utf8.starts_with("file:"))
        throw std::invalid_argument("A local database file path is required (not a SQLite URI)");
    if (game_id.empty() || game_id == "global")
        throw std::invalid_argument("A nonempty game id distinct from global is required");
    sqlite3* raw = nullptr;
    const int opened = sqlite3_open_v2(database_path_utf8.c_str(), &raw,
        SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nullptr);
    impl_->connection.reset(raw); // Own even a partially opened SQLite handle.
    check(raw, opened, "open read-only corpus");
    check(raw, sqlite3_extended_result_codes(raw, 1), "enable SQLite result codes");
    check(raw, sqlite3_busy_timeout(raw, std::clamp(busy_timeout_ms, 0, 10000)), "set read timeout");
    if (sqlite3_db_readonly(raw, "main") != 1)
        throw CorpusError("Corpus connection must be read-only", SQLITE_READONLY);
    impl_->game = std::move(game_id);
    Statement game(raw, "SELECT 1 FROM games WHERE id=?");
    game.bind(1, impl_->game);
    if (!game.row()) throw std::invalid_argument("Game id is not present in this corpus");
}

Corpus::~Corpus() = default;
Corpus::Corpus(Corpus&&) noexcept = default;
Corpus& Corpus::operator=(Corpus&&) noexcept = default;
Corpus::Impl& Corpus::impl() const {
    if (!impl_) throw std::logic_error("Corpus was moved from");
    return *impl_;
}
const std::string& Corpus::game_id() const { return impl().game; }

SearchPage Corpus::search(const SearchRequest& request) const {
    validate(request.query, 16384, "Search query");
    validate(request.category, 4096, "Category");
    auto words = tokens(request.query);
    SearchPage page;
    page.limit = std::clamp(request.limit, 1, 500);
    page.offset = std::max<std::int64_t>(request.offset, 0);
    page.tokens_truncated = words.size() > 12;
    if (words.size() > 12) words.resize(12);
    auto& state = impl();
    std::lock_guard lock(state.mutex);
    auto* db = state.connection.get();
    std::string where = " WHERE game_id=?";
    std::vector<std::string> parameters{state.game};
    if (!request.category.empty()) { where += " AND category=?"; parameters.push_back(request.category); }
    for (const auto& word : words) {
        where += " AND document LIKE ? ESCAPE '\\'";
        parameters.push_back(literal_pattern(word));
    }
    const auto bind = [&](Statement& statement) {
        int index = 1;
        for (const auto& parameter : parameters) statement.bind(index++, parameter);
        return index;
    };
    ReadSnapshot snapshot(db);
    {
        Statement count(db, "SELECT count(*) FROM entries" + where);
        bind(count);
        if (count.row()) page.total = count.integer(0);
    }
    {
        Statement rows(db, std::string("SELECT ") + kEntryColumns + " FROM entries" + where +
            " ORDER BY category,key,id LIMIT ? OFFSET ?");
        int index = bind(rows);
        rows.bind(index++, static_cast<std::int64_t>(page.limit));
        rows.bind(index, page.offset);
        while (rows.row()) page.items.push_back(read_entry(rows));
    }
    snapshot.commit();
    return page;
}

std::optional<Entry> Corpus::entry(std::string_view entry_id) const {
    validate(entry_id, 4096, "Entry id");
    auto& state = impl();
    std::lock_guard lock(state.mutex);
    Statement query(state.connection.get(), std::string("SELECT ") + kEntryColumns +
        " FROM entries WHERE id=? AND game_id=?");
    query.bind(1, entry_id); query.bind(2, state.game);
    if (!query.row()) return std::nullopt;
    return read_entry(query);
}

std::vector<std::string> Corpus::categories() const {
    auto& state = impl();
    std::lock_guard lock(state.mutex);
    Statement query(state.connection.get(), "SELECT DISTINCT category FROM entries WHERE game_id=? ORDER BY category");
    query.bind(1, state.game);
    std::vector<std::string> result;
    while (query.row()) result.push_back(query.text(0));
    return result;
}

std::vector<LookupHit> Corpus::lookup(std::string_view term, bool allow_variants) const {
    validate(term, 4096, "Lookup term");
    const auto normalized = normalize(term);
    if (normalized.empty()) return {};
    auto& state = impl();
    std::lock_guard lock(state.mutex);
    auto* db = state.connection.get();
    ReadSnapshot snapshot(db);
    auto result = lookup_definitions(db, state.game, normalized, allow_variants);
    snapshot.commit();
    return result;
}

std::vector<LookupHit> Corpus::sentenceWords(std::string_view en, std::size_t max_words) const {
    validate(en, 128 * 1024, "Sentence");
    max_words = std::min<std::size_t>(max_words, 120);
    if (!max_words) return {};
    const auto words = sentence_tokens(sentence_text(en), 2048);
    auto& state = impl();
    std::lock_guard lock(state.mutex);
    auto* db = state.connection.get();
    ReadSnapshot snapshot(db);
    std::unordered_set<std::string> seen;
    std::vector<LookupHit> result;
    for (const auto& token : words) {
        const auto normalized = normalize(token.word);
        if (!seen.insert(normalized).second) continue;
        auto matches = normalized.size() <= 128 ? lookup_definitions(db, state.game, normalized, true) : std::vector<LookupHit>{};
        LookupHit hit;
        if (matches.empty()) {
            hit.meaning = "本地词典未收录，可能是专名、游戏造词或词形变化。";
            hit.source = "未收录";
        } else hit = std::move(matches.front());
        hit.term = token.word;
        result.push_back(std::move(hit));
        if (result.size() == max_words) break;
    }
    snapshot.commit();
    return result;
}

std::vector<LookupHit> Corpus::sentencePhrases(std::string_view en, std::size_t max_phrases) const {
    validate(en, 128 * 1024, "Sentence");
    max_phrases = std::min<std::size_t>(max_phrases, 100);
    if (!max_phrases) return {};
    auto& state = impl();
    std::lock_guard lock(state.mutex);
    auto* db = state.connection.get();
    ReadSnapshot snapshot(db);
    auto result = automatic_phrases(db, state.game, en, max_phrases);
    snapshot.commit();
    return result;
}

std::vector<LookupHit> Corpus::entryPhrases(const Entry& selected, std::size_t max_phrases) const {
    auto& state = impl();
    if (selected.game_id != state.game)
        throw std::invalid_argument("Entry annotations belong to a different game");
    validate(selected.id, 4096, "Entry id");
    validate(selected.en, 128 * 1024, "Sentence");
    validate(selected.phrases_json, 1024 * 1024, "Entry phrase JSON");
    max_phrases = std::min<std::size_t>(max_phrases, 100);
    if (!max_phrases) return {};
    std::lock_guard lock(state.mutex);
    auto* db = state.connection.get();
    ReadSnapshot snapshot(db);
    {
        Statement owner(db, "SELECT 1 FROM entries WHERE id=? AND game_id=?");
        owner.bind(1, selected.id); owner.bind(2, state.game);
        if (!owner.row()) throw std::invalid_argument("Entry no longer exists in the selected game");
    }
    std::vector<LookupHit> result;
    std::unordered_set<std::string> seen;
    if (!selected.phrases_json.empty()) {
        {
            Statement valid(db, "SELECT json_valid(?1), CASE WHEN json_valid(?1) THEN json_type(?1) ELSE '' END");
            valid.bind(1, selected.phrases_json);
            if (!valid.row() || valid.integer(0) != 1 || valid.text(1) != "array")
                throw std::invalid_argument("Entry phrase notes must be a valid JSON array");
        }
        Statement notes(db,
            "SELECT json_extract(obj,'$.term'),json_extract(obj,'$.meaning'),json_extract(obj,'$.source'),"
            "json_type(obj,'$.term'),json_type(obj,'$.meaning'),json_type(obj,'$.source') "
            "FROM (SELECT CASE WHEN type='object' THEN value ELSE '{}' END AS obj,"
            "CAST(key AS INTEGER) AS ordinal FROM json_each(?1)) ORDER BY ordinal LIMIT 512");
        notes.bind(1, selected.phrases_json);
        while (result.size() < max_phrases && notes.row()) {
            if (notes.text(3) != "text" || notes.text(4) != "text") continue;
            auto term = notes.text(0), meaning = notes.text(1);
            validate(term, 4096, "Annotation term");
            validate(meaning, 128 * 1024, "Annotation meaning");
            const auto normalized = normalize(term);
            if (normalized.empty() || tokens(meaning).empty() || !seen.insert(normalized).second) continue;
            auto source = notes.text(5) == "text" ? notes.text(2) : std::string{};
            validate(source, 16384, "Annotation source");
            if (source.empty()) source = "原句注释（未注明来源）";
            result.push_back({std::move(term), std::move(meaning), std::move(source),
                              state.game, DefinitionKind::Phrase, false, true});
        }
    }
    if (result.size() < max_phrases) {
        auto automatic = automatic_phrases(db, state.game, selected.en, max_phrases);
        for (auto& hit : automatic) {
            if (!seen.insert(normalize(hit.term)).second) continue;
            result.push_back(std::move(hit));
            if (result.size() == max_phrases) break;
        }
    }
    snapshot.commit();
    return result;
}

} // namespace bilingual::data
