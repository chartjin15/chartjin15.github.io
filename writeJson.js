// 使用说明：
// - 运行该脚本需要使用`node`环境；
// - 命令行启动时若什么参数都不带，默认用“对象文本_六位日期”命名；
// - 命令行启动时输入“文件路径名”，则使用输入的文件名命名。

const fs = require('fs');
const readline = require('readline');


function OneLineContent(id, content) {
    this.id = id;
    this.content = content;
    this.timestamp = Date.now();
}

function getDateString(timezone = 8) {
    timeFormat = new Intl.DateTimeFormat('zh-CN', {
        year: '2-digit',
        month: '2-digit',
        day: '2-digit',
        timeZone: `Etc/GMT${timezone > 0 ? '-' : '+'}${Math.abs(timezone)}`
    });

    return timeFormat.format(new Date()).replaceAll('/', '');
}

function getFilePath(defaultName = '对象文本') {
    let inputFilePath = process.argv[2];
    let filePathToUse = (inputFilePath === undefined) ? defaultName + '_' + getDateString() : inputFilePath;
    return './' + filePathToUse + '.json';
}

function readJson(jsonPath) {
    let folderName = jsonPath.substring(0, jsonPath.lastIndexOf('/'));
    if (!fs.existsSync(folderName)) {
        fs.mkdirSync(folderName, {
            recursive: true
        });
    }

    if (!fs.existsSync(jsonPath)) {
        fs.writeFileSync(jsonPath, '[]', 'utf-8');
    }

    let jsonFile = fs.readFileSync(jsonPath, 'utf-8');
    let jsonData = JSON.parse(jsonFile);

    return jsonData;
}

function updateJson(jsonPath, newContent) {
    let jsonData = readJson(jsonPath);

    let newId = jsonData.length ? (Math.max(...jsonData.map(item => item.id)) + 1) : 1;
    jsonData.push(new OneLineContent(newId, newContent));

    fs.writeFileSync(jsonPath, JSON.stringify(jsonData, null, 2), 'utf-8');
}

function cliInput() {
    rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout
    });

    function getInput() {
        rl.question('请输入一句话（输入“q”退出）：', (input) => {
            if (input.toLowerCase() === 'q') {
                rl.close();
                return;
            }

            updateJson(getFilePath(), input);

            getInput();
        });
    }

    getInput();
}

cliInput();
