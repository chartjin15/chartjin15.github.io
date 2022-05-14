try:
    with open("./files/manga_tracking.csv", encoding="UTF-8") as fileobj:
        lines = fileobj.read().splitlines()

        read_it = []
        for line in lines:
            line = str(line).split(",")
            if line[2] == "1" and line[4] == line[5]:
                read_it.append(line[0])

        for ch_name in read_it:
            print(ch_name)
except FileNotFoundError:
    print("Missing file!")
