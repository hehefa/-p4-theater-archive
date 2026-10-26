# P4与何发：公开文字资料

[字幕下载](captions.md) · [文稿与研究](materials.md) · [公众号原文](wechat.md) · [打卡进度](../PROGRESS.md)

当前收录487份字幕、685项文稿／资料记录、64项研究。不同语言、字节版本、文章、素材和作品分别登记，数量不混作作品总数。

文字保留原字节或有来源的公开访问版、准确署名与原文链接。何发个人作品保持个人归属。大型影音保留官方观看／下载入口，内部底稿、原片与私有运维信息不在公开包。

[逐项来源与版本](manifest.json) · [版本批准记录](approvals.json) · [公开范围规则](../docs/公开范围投影.md)

## 检查与检索

```sh
python -X utf8 scripts/official_publications.py check --root .
python -X utf8 scripts/official_publications.py build-indexes --root .
python -X utf8 scripts/official_publications.py search "剧本" --root .
```

检索带原文链接、字幕cue、版本SHA与相应时间轴。公开访问不授予统一开放许可；权利和核验范围沿用原资料。
