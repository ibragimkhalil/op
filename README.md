# Автоматизация. Команда Ctrl+C Ctrl+V Heroes

## Трассировка нормативной цепочки A → B → C

Используется: Ollama (локальная модель, полностью бесплатно, без регистрации)

Установка:\
    1. Скачать Ollama: https://ollama.com → установить\
    2. В CMD: `ollama pull llama3.1`\
    3. `pip install python-docx pdfplumber requests`

Запуск:\
     `python main.py --doc_a A.pdf --doc_b B.pdf --doc_c C.pdf --model llama3.1`
