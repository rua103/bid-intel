# 附件回归样例

legacy-quotation.doc 是合成的 Word 97–2003 二进制文件（OLE magic: d0cf11e0a1b11ae1），不含真实公告数据。用 python-docx 创建两行七列表格，再通过 Microsoft Word 16 的 Word 97–2003 格式保存，保存前移除文档个人信息。正式解析只需要 LibreOffice，不依赖 Microsoft Word。

表头为：采购标的、品目名称、品牌、规格型号、数量、单价、总价。

数据为：打印机、办公设备、示例牌、X-100、2台、1000、2000。

test_attachment_readiness.py 用实际 LibreOffice 转回 DOCX 并核对七字段；未安装时只跳过这个系统依赖集成用例，缺依赖及超时提示仍有测试。XLS 和中文 PDF 在测试中由 xlwt、reportlab 动态生成。

一次实际兼容性发现：LibreOffice 26.2.6.3 自身通过 MS Word 97 过滤器生成的同内容 DOC，在 Word 中可见表格，但由 LibreOffice 重新导入时变成连续文字。该样例不用于证明七字段支持；代码对“DOC 有文本但无标的表格”显式警告。任意生成器的旧 DOC、复杂布局仍需实物核验。
