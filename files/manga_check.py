try:
    with open("./files/manga_tracking.csv", encoding="UTF-8") as fileobj:
        lines = fileobj.read().splitlines()

        status = [[], [], [], []]
        for line in lines[1:]:
            line = str(line).split(",")
            if line[2] == "2":
                status[3].append(line[0] + "\t最终读至第" + line[5] + "话")
            elif line[2] == "0":
                if line[4] != line[5] and line[5] != "0":
                    status[0].append(line[0] + "\t仍差" + str(int(line[4]) - int(line[5])) + "话")
            else:
                if line[4] == line[5]:
                    status[2].append(line[0] + "\t已读完")
                else:
                    status[1].append(line[0] + "\t仍差" + str(int(line[4]) - int(line[5])) + "话")

        print("目前未读完的连载日本漫画作品：")
        for ch_name in status[0]:
            print("- " + ch_name)
        print("\n目前未读完的完结日本漫画作品：")
        for ch_name in status[1]:
            print("- " + ch_name)
        print("\n目前已读完的完结日本漫画作品：")
        for ch_name in status[2]:
            print("- " + ch_name)
        print("\n目前已弃坑的难看日本漫画作品：")
        for ch_name in status[3]:
            print("- " + ch_name)
except FileNotFoundError:
    print("Missing file!")
