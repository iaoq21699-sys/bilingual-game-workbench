BilingualPanel 0.7.0 · Windows x64

工作台原生检索插件，使用 ReShade 6.8.0 Full Add-on 和 API 20。
检查、安装和恢复通过工作台「面板设置」和「游戏」菜单进行。
Windows 便携包内有编译好的 BilingualPanel.addon64。
ReShade 运行库从 https://reshade.me/ 获取。用 prepare-native-panel.cmd
准备运行库，或放入工作台 tools/reshade-runtime/ReShade64.dll。

源码构建见 docs/build.md。产物在 native/build/bin/Release。
SDK、ImGui 和 SQLite 版本与许可详见 native/vendor/README.txt。
游戏目录中的 BilingualPanel.ini 保存本机数据库路径和主题。
迁移工作台后需确认 Database 路径，并重新生成双语启动入口。