const fs = require('fs');
const readline = require('readline').createInterface({
    input: process.stdin,
    output: process.stdout
});

let modifyFilepath = './' + process.argv[2] + '.json';

function OneLineContent(id, content) {
    this.id = id;
    this.content = content;
    this.timestamp = Date.now();
}

function updateJson(jsonPath, newContent) {
    if (!fs.existsSync(jsonPath)) {
        fs.writeFileSync(jsonPath, '[]', 'utf-8');
    }

    let jsonFile = fs.readFileSync(jsonPath, 'utf-8');
    let jsonData = JSON.parse(jsonFile);

    let newId = jsonData.length ? (Math.max(...jsonData.map(item => item.id)) + 1) : 1;
    jsonData.push(new OneLineContent(newId, newContent));

    fs.writeFileSync(jsonPath, JSON.stringify(jsonData, null, 2), 'utf-8');
}

function cliInput() {
    readline.question('请输入一句话（输入“q”退出）：', (input) => {
        if (input.toLowerCase() === 'q') {
            readline.close();
            return;
        }

        updateJson(modifyFilepath, input);
        
        cliInput();
    });
}

cliInput();