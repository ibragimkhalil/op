# Автоматизация. Команда Ctrl+C Ctrl+V Heroes

## Трассировка нормативной цепочки A → B → C
Онтологическая целостность трёхуровневых документов

Использует: Ollama (локальная модель, полностью бесплатно, без регистрации)

Установка:
    1. Скачай Ollama: https://ollama.com → установи
    2. В CMD: ollama pull phi3
    3. pip install python-docx pdfplumber requests

Запуск:
     python main.py --doc_a A.pdf --doc_b B.pdf --doc_c C.pdf --model phi3
