# 留言板接入说明

`appstore.json` 现在新增根级字段 `messageBoard`，和 `apps` 同级。
已有应用列表解析逻辑不需要改动，只需要在原来的顶层数据模型里补一个字段。

## JSON 结构

```json
{
  "notice": "公告内容",
  "categories": ["工具", "导航地图"],
  "apps": [],
  "messageBoard": [
    {
      "title": "影视仓视频源",
      "contents": [
        {
          "text": "讴歌\n附带直播",
          "canCopy": false
        },
        {
          "text": "http://tv.nxog.top/m/",
          "canCopy": true
        }
      ]
    }
  ]
}
```

## 字段说明

- `messageBoard`：留言板列表。没有该字段时按空列表处理。
- `messageBoard[].title`：一组留言的标题。
- `messageBoard[].contents`：标题下的内容列表，按数组顺序展示。
- `contents[].text`：展示文本，可能包含换行。
- `contents[].canCopy`：是否允许复制。`true` 时前端可以显示复制按钮。

## Kotlin 数据模型示例

如果当前已经有顶层 `AppStoreData` 或类似模型，直接加 `messageBoard` 字段：

```kotlin
data class AppStoreData(
    val notice: String? = null,
    val categories: List<String> = emptyList(),
    val apps: List<AppInfo> = emptyList(),
    val messageBoard: List<MessageBoardItem> = emptyList()
)

data class MessageBoardItem(
    val title: String = "",
    val contents: List<MessageContent> = emptyList()
)

data class MessageContent(
    val text: String = "",
    val canCopy: Boolean = false
)
```

## 展示逻辑

推荐按原始顺序展示，不要重新排序：

```kotlin
data.messageBoard.forEach { item ->
    showTitle(item.title)
    item.contents.forEach { content ->
        showText(content.text)
        if (content.canCopy) {
            showCopyButton(content.text)
        }
    }
}
```

## 兼容规则

- `messageBoard` 缺失：显示空留言板。
- `contents` 缺失或为空：只展示标题，或跳过该留言块。
- `text` 为空：建议跳过该内容。
- `canCopy` 缺失：按 `false` 处理。

## 当前数据特点

当前 `contents` 常见为两条一组：

```json
[
  { "text": "名称或说明", "canCopy": false },
  { "text": "可复制链接或内容", "canCopy": true }
]
```

客户端不强制按两条一组解析。最稳妥的方式仍然是逐条按顺序展示，只在 `canCopy=true` 时提供复制能力。
