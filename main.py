import argparse
import json
import os
import sys
import time
import re
import requests
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
#  Настройки
# ─────────────────────────────────────────────

OLLAMA_URL  = "http://localhost:11434/api/generate"
MODEL_NAME  = ""

# ─────────────────────────────────────────────
#  Чтение документов
# ─────────────────────────────────────────────

def read_docx(path: str) -> str:
    try:
        import docx
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        raise RuntimeError("Установите: pip install python-docx")

def read_pdf(path: str) -> str:
    try:
        import pdfplumber
        text = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text.append(t)
        return "\n".join(text)
    except ImportError:
        raise RuntimeError("Установите: pip install pdfplumber")

def read_document(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return read_docx(path)
    elif ext == ".pdf":
        return read_pdf(path)
    elif ext in (".txt", ".md"):
        with open(path, encoding="utf-8") as f:
            return f.read()
    else:
        raise ValueError(f"Неподдерживаемый формат: {ext}")


# ─────────────────────────────────────────────
#  Промпты — цитаты короткие, JSON компактный
# ─────────────────────────────────────────────

EXTRACT_PROMPT = """Ты — эксперт по нормативным документам. Отвечай ТОЛЬКО валидным JSON, без пояснений, без markdown.

Извлеки 8 ключевых понятий из документа. Поле "quote" — не более 10 слов.

Формат ответа:
{{"concepts":[{{"id":"C1","name":"понятие","section":"пункт","quote":"короткая цитата"}},{{"id":"C2","name":"понятие","section":"пункт","quote":"короткая цитата"}}]}}

ДОКУМЕНТ:
{doc_a}
"""

TRACE_AB_PROMPT = """Ты — эксперт по анализу документов. Отвечай ТОЛЬКО валидным JSON, без пояснений, без markdown.

Для каждого понятия найди реализацию в документе B. Поля quote_a и quote_b — не более 10 слов каждое.

Формат ответа:
{{"trace_ab":[{{"id":"C1","name":"понятие","section_a":"пункт","quote_a":"цитата","section_b":"пункт или нет","quote_b":"цитата или нет","match":"да|частично|нет","gap_type":"Т|Л|С|нет","gap_note":"описание или нет"}}]}}

Типы разрывов: Т=терминологический, Л=логический, С=структурный

ПОНЯТИЯ:
{concepts_json}

ДОКУМЕНТ B:
{doc_b}
"""

TRACE_BC_PROMPT = """Ты — эксперт по анализу документов. Отвечай ТОЛЬКО валидным JSON, без пояснений, без markdown.

Для каждого понятия найди реализацию в документе C. Поля quote_b и quote_c — не более 10 слов каждое.

Формат ответа:
{{"trace_bc":[{{"id":"C1","section_b":"пункт","quote_b":"цитата","section_c":"пункт или нет","quote_c":"цитата или нет","match_bc":"да|частично|нет","gap_type_bc":"Т|Л|С|нет","gap_note_bc":"описание или нет"}}]}}

ТРАССИРОВКА A→B:
{trace_ab_json}

ДОКУМЕНТ C:
{doc_c}
"""

SUMMARY_PROMPT = """Ты — эксперт по анализу документов. Отвечай ТОЛЬКО валидным JSON, без пояснений, без markdown.

Сделай итоговый анализ трассировки A→B→C.

Формат ответа:
{{"overall_integrity":"высокая|средняя|низкая","integrity_score":75,"items":[{{"id":"C1","name":"понятие","chain_verdict":"сохранена|частично|нарушена","critical":false,"recommendation":"что исправить"}}],"critical_gaps":["разрыв 1"],"general_recommendations":["рекомендация 1"]}}

ТРАССИРОВКА A→B:
{trace_ab_json}

ТРАССИРОВКА B→C:
{trace_bc_json}
"""


# ─────────────────────────────────────────────
#  Восстановление обрезанного JSON
# ─────────────────────────────────────────────

def repair_json(raw: str) -> str:
    """Пытается починить обрезанный JSON — закрывает незакрытые скобки/кавычки."""
    # Убираем хвостовой мусор после последней закрывающей скобки массива/объекта
    raw = raw.strip()

    # Считаем баланс скобок
    open_braces   = raw.count('{') - raw.count('}')
    open_brackets  = raw.count('[') - raw.count(']')

    # Если JSON обрезан внутри строки — обрезаем до последней полной записи
    # Ищем последнюю полную запись объекта в массиве: }}
    if open_braces > 0 or open_brackets > 0:
        # Откатываемся до последней закрытой записи
        last_good = max(raw.rfind('}'), 0)
        if last_good > 0:
            raw = raw[:last_good + 1]
            # Пересчитываем
            open_braces   = raw.count('{') - raw.count('}')
            open_brackets  = raw.count('[') - raw.count(']')

        # Закрываем что осталось
        raw += ']' * open_brackets
        raw += '}' * open_braces

    return raw


# ─────────────────────────────────────────────
#  Вызов Ollama
# ─────────────────────────────────────────────

def check_ollama():
    try:
        requests.get("http://localhost:11434", timeout=3)
        return True
    except Exception:
        return False

def call_ollama(prompt: str, label: str) -> dict:
    print(f"  [{label}] Генерация...")

    for attempt in range(3):
        try:
            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": MODEL_NAME,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 2048,
                    }
                },
                timeout=600
            )
            response.raise_for_status()
            raw = response.json().get("response", "").strip()

            # Убираем markdown-обёртку
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            # Ищем JSON-блок в тексте
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                raw = json_match.group()

            # Пробуем распарсить напрямую
            try:
                result = json.loads(raw)
                print(f"  [{label}] ✓ Готово")
                return result
            except json.JSONDecodeError:
                # Пробуем починить обрезанный JSON
                repaired = repair_json(raw)
                try:
                    result = json.loads(repaired)
                    print(f"  [{label}] ✓ Готово (JSON восстановлен)")
                    return result
                except json.JSONDecodeError as e:
                    print(f"  [{label}] Ошибка парсинга JSON (попытка {attempt+1}/3): {e}")
                    print(f"  Ответ модели: {raw[:200]}...")
                    if attempt == 2:
                        raise RuntimeError(
                            f"Модель вернула некорректный JSON на шаге '{label}'.\n"
                            f"Попробуйте уменьшить документы или использовать модель побольше."
                        )
                    time.sleep(2)

        except RuntimeError:
            raise

        except requests.exceptions.ConnectionError:
            print(f"\nОШИБКА: Ollama не запущена!")
            print("Запустите Ollama или выполните в CMD: ollama serve")
            sys.exit(1)

        except Exception as e:
            print(f"  [{label}] Ошибка (попытка {attempt+1}/3): {e}")
            if attempt == 2:
                raise
            time.sleep(3)


# ─────────────────────────────────────────────
#  Объединение результатов
# ─────────────────────────────────────────────

def merge_results(concepts, trace_ab, trace_bc, summary) -> list:
    ab_map  = {item["id"]: item for item in trace_ab}
    bc_map  = {item["id"]: item for item in trace_bc}
    sum_map = {item["id"]: item for item in summary.get("items", [])}

    rows = []
    for c in concepts:
        cid = c["id"]
        ab  = ab_map.get(cid, {})
        bc  = bc_map.get(cid, {})
        sm  = sum_map.get(cid, {})
        rows.append({
            "id":            cid,
            "name":          c.get("name", ""),
            "section_a":     c.get("section", ""),
            "quote_a":       c.get("quote", ""),
            "section_b":     ab.get("section_b", ""),
            "quote_b":       ab.get("quote_b", ""),
            "match_ab":      ab.get("match", ""),
            "gap_type_ab":   ab.get("gap_type", ""),
            "gap_note_ab":   ab.get("gap_note", ""),
            "section_c":     bc.get("section_c", ""),
            "quote_c":       bc.get("quote_c", ""),
            "match_bc":      bc.get("match_bc", ""),
            "gap_type_bc":   bc.get("gap_type_bc", ""),
            "gap_note_bc":   bc.get("gap_note_bc", ""),
            "chain_verdict": sm.get("chain_verdict", ""),
            "critical":      sm.get("critical", False),
            "recommendation": sm.get("recommendation", ""),
        })
    return rows


# ─────────────────────────────────────────────
#  Генерация отчётов
# ─────────────────────────────────────────────

MATCH_EMOJI   = {"да": "да", "частично": "частично", "нет": "нет", "": "—"}
VERDICT_EMOJI = {"сохранена": "сохранена", "частично": "частично", "нарушена": "нарушена", "": "—"}

def q(text, limit=120):
    s = str(text)
    return s[:limit] + ("…" if len(s) > limit else "")

def save_json(rows, summary, output_dir, ts):
    path = os.path.join(output_dir, "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "generated_at": datetime.now().isoformat(),
                "model": MODEL_NAME,
                "total_concepts": len(rows),
                "integrity": summary.get("overall_integrity"),
                "integrity_score": summary.get("integrity_score"),
            },
            "summary": summary,
            "trace_table": rows,
        }, f, ensure_ascii=False, indent=2)
    return path

def save_markdown(rows, summary, doc_a, doc_b, doc_c, output_dir, ts):
    path = os.path.join(output_dir, "result.md")
    lines = [
        "# Трассировка нормативной цепочки A → B → C",
        f"*Сформирован: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Модель: {MODEL_NAME}*\n",
        f"**A:** `{Path(doc_a).name}` → **B:** `{Path(doc_b).name}` → **C:** `{Path(doc_c).name}`\n",
        f"**Онтологическая целостность:** {summary.get('overall_integrity','—').upper()}  ",
        f"**Индекс:** {summary.get('integrity_score','—')} / 100\n",
        "---",
    ]
    gaps = summary.get("critical_gaps", [])
    if gaps:
        lines += ["### Критические разрывы"] + [f"- {g}" for g in gaps] + [""]
    recs = summary.get("general_recommendations", [])
    if recs:
        lines += ["### Рекомендации"] + [f"- {r}" for r in recs] + [""]

    lines += [
        "## Таблица трассировки\n",
        "| ID | Понятие | A: пункт | A→B | B: пункт | Разрыв A→B | B→C | C: пункт | Разрыв B→C | Итог | Крит. | Рекомендация |",
        "|---|---|---|:---:|---|---|:---:|---|---|---|:---:|---|",
    ]
    for r in rows:
        crit = "ДА" if r["critical"] else "—"
        lines.append(
            f"| {r['id']} | {r['name']} "
            f"| {r['section_a']} "
            f"| {MATCH_EMOJI.get(r['match_ab'], r['match_ab'])} "
            f"| {r['section_b']} "
            f"| {r['gap_type_ab']}: {q(r['gap_note_ab'],50)} "
            f"| {MATCH_EMOJI.get(r['match_bc'], r['match_bc'])} "
            f"| {r['section_c']} "
            f"| {r['gap_type_bc']}: {q(r['gap_note_bc'],50)} "
            f"| {VERDICT_EMOJI.get(r['chain_verdict'],'')} {r['chain_verdict']} "
            f"| {crit} | {q(r['recommendation'],80)} |"
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

# ─────────────────────────────────────────────
#  Главная функция
# ─────────────────────────────────────────────

def run_trace(doc_a_path, doc_b_path, doc_c_path, output_dir="output", model=MODEL_NAME):
    global MODEL_NAME
    MODEL_NAME = model
    print("\n" + "="*60)
    print("  ТРАССИРОВКА НОРМАТИВНОЙ ЦЕПОЧКИ A → B → C")
    print(f"  Модель: {MODEL_NAME} (локально через Ollama)")
    print("="*60)

    for label, path in [("A", doc_a_path), ("B", doc_b_path), ("C", doc_c_path)]:
        if not os.path.exists(path):
            print(f"\nОШИБКА: Файл '{path}' (документ {label}) не найден.")
            sys.exit(1)

    print("\n[0/5] Проверка Ollama...")
    if not check_ollama():
        print("ОШИБКА: Ollama не запущена.")
        print("Открой приложение Ollama или выполни в CMD: ollama serve")
        sys.exit(1)
    print("  ✓ Ollama работает")

    print("\n[1/5] Чтение документов...")
    doc_a = read_document(doc_a_path)
    doc_b = read_document(doc_b_path)
    doc_c = read_document(doc_c_path)
    print(f"  A: {len(doc_a):,} символов | B: {len(doc_b):,} | C: {len(doc_c):,}")

    # Для маленьких моделей — урезаем контекст
    A = doc_a[:4000]
    B = doc_b[:4000]
    C = doc_c[:4000]

    print("\n[2/5] Извлечение ключевых понятий из документа A...")
    extract = call_ollama(EXTRACT_PROMPT.format(doc_a=A), "Извлечение")
    concepts = extract.get("concepts", [])
    print(f"  Найдено понятий: {len(concepts)}")

    print("\n[3/5] Трассировка A → B...")
    ab = call_ollama(TRACE_AB_PROMPT.format(
        concepts_json=json.dumps(concepts, ensure_ascii=False, indent=2),
        doc_b=B
    ), "A→B")
    trace_ab = ab.get("trace_ab", [])

    print("\n[4/5] Трассировка B → C...")
    bc = call_ollama(TRACE_BC_PROMPT.format(
        trace_ab_json=json.dumps(trace_ab, ensure_ascii=False, indent=2),
        doc_c=C
    ), "B→C")
    trace_bc = bc.get("trace_bc", [])

    print("\n[5/5] Итоговый анализ цепочки A→B→C...")
    summary = call_ollama(SUMMARY_PROMPT.format(
        trace_ab_json=json.dumps(trace_ab, ensure_ascii=False, indent=2),
        trace_bc_json=json.dumps(trace_bc, ensure_ascii=False, indent=2),
    ), "Итог")

    rows = merge_results(concepts, trace_ab, trace_bc, summary)
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = save_json(rows, summary, output_dir, ts)
    md_path   = save_markdown(rows, summary, doc_a_path, doc_b_path, doc_c_path, output_dir, ts)

    critical = sum(1 for r in rows if r.get("critical"))
    print("\n" + "="*60)
    print("  РЕЗУЛЬТАТЫ")
    print("="*60)
    print(f"  Онтологическая целостность: {summary.get('overall_integrity','—').upper()}")
    print(f"  Индекс целостности:         {summary.get('integrity_score','—')} / 100")
    print(f"  Понятий проанализировано:   {len(rows)}")
    print(f"  Критических разрывов:       {critical}")
    print(f"\n  JSON:     {json_path}")
    print(f"  Markdown: {md_path}")
    print("="*60 + "\n")


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Трассировка нормативной цепочки A→B→C (Ollama, локально, бесплатно)"
    )
    parser.add_argument("--doc_a",  required=True, help="Документ A — верхний уровень (закон, стандарт)")
    parser.add_argument("--doc_b",  required=True, help="Документ B — средний уровень (регламент, политика)")
    parser.add_argument("--doc_c",  required=True, help="Документ C — нижний уровень (процедура, инструкция)")
    parser.add_argument("--output", default="output", help="Папка для отчётов (по умолчанию: output)")
    parser.add_argument("--model",  default="llama3.1", help="Модель Ollama (по умолчанию: llama3.1)")
    args = parser.parse_args()

    run_trace(args.doc_a, args.doc_b, args.doc_c, args.output, args.model)

if __name__ == "__main__":
    main()
