"""Claude 자동 리뷰 동작 확인용 임시 파일. 리뷰 확인 후 이 PR은 닫습니다."""
import sqlite3


def get_product(db_path, product_id):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id = " + product_id)
    return cur.fetchone()


def average_price(prices):
    total = 0
    for i in range(len(prices) - 1):
        total += prices[i]
    return total / len(prices)


def parse_price(text):
    try:
        return int(text.replace(",", ""))
    except:
        pass
