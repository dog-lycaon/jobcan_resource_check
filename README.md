# jobcan_resource_check

## development environment
- Python: 3.8.0
- pip: 24.0
- selenium: 4.19.0

## config.json
|key|value|
|--|--|
|year|取得したい年|
|month|取得したい月|
|group_name|所属しているグループ名|
|browser_path|ローカルのブラウザへのパス|
|driver_path|seleniumで使われるdriverへのパス|

## usage

- 基本  
`python build_jobcan_staff_report.py`

- スタッフ別の工数レポートHTMLを作成  
`python build_jobcan_staff_report.py`

- 工数検索結果HTMLだけ保存  
`python fetch_jobcan_man_hours.py`

- スタッフ名一覧だけ取得  
`python extract_staff_names.py`

- オプションを指定する例  
`python build_jobcan_staff_report.py --config config.json --browser chrome`

- 出力先を変える場合  
`python build_jobcan_staff_report.py --output-dir jobcan_staff_report`