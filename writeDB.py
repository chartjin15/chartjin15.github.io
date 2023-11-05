import datetime, readline, sqlite3, sys

connect = sqlite3.connect(("对象文本" if len(sys.argv) < 2 else sys.argv[1]) + ".db")
cursor = connect.cursor()

cursor.execute(
    """CREATE TABLE IF NOT EXISTS text (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        record_time TEXT NOT NULL)
    """
)

line_index = 0
while True:
    line_index += 1
    input_string = input("{:04}| ".format(line_index))
    if input_string in ["q", "quit"]:
        connect.close()
        break
    cursor.execute(
        "INSERT INTO text (content, record_time) VALUES (?, ?)",
        (
            input_string,
            datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=8))
            ).strftime("%Y/%m/%d %H:%M:%S"),
        ),
    )
    connect.commit()
