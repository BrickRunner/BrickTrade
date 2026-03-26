from playwright.sync_api import sync_playwright
from rich.console import Console

console = Console()

# Селекторы (уточнены под Arena AI)
PROMPT_SELECTOR = 'textarea'  # поле ввода
BUTTON_SELECTOR = 'button[type="submit"]'  # кнопка отправки
RESPONSE_SELECTOR = 'div[class*="response"]'  # блок с ответом (пример, уточни через DevTools)

def main():
    console.print("[bold green]Запуск браузера...[/bold green]")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)  # slow_mo для отладки
        context = browser.new_context()
        page = context.new_page()
        
        page.goto("https://arena.ai")  
        console.print("[bold blue]Авторизуйся вручную и нажми Enter[/bold blue]")
        input()  # ждем входа
        
        while True:
            prompt = console.input("\n[bold yellow]Введите промпт: [/bold yellow]")
            if prompt.lower() in ["exit", "quit"]:
                break
            
            # Вводим текст
            # Вводим текст
            page.fill(PROMPT_SELECTOR, prompt)

            # Ждем, пока кнопка станет активной
            page.wait_for_function(
                """(selector) => {
                    const btn = document.querySelector(selector);
                    return btn && !btn.disabled;
                }""",
                arg=BUTTON_SELECTOR,
                timeout=10000
            )

            # Кликаем кнопку
            page.click(BUTTON_SELECTOR)
            
            console.print("[bold green]Ждем ответ...[/bold green]")
            page.wait_for_selector(RESPONSE_SELECTOR, timeout=30000)
            response = page.query_selector(RESPONSE_SELECTOR).inner_text()
            
            console.print(f"[bold cyan]Claude Opus:[/bold cyan] {response}")
        
        browser.close()

if __name__ == "__main__":
    main()