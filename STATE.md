# 当前状态 (STATE.md)

> 每次工作会话**结束时**更新本文件。读它 = 立刻知道项目停在哪、下一步干啥。

---

## 当前阶段

**Phase 0 — 环境与脚手架**

---

## 已完成

- [x] GitHub 私有仓库 `a-share-quant` 建立
- [x] clone 到本地，README / .gitignore(Python) 就位
- [x] PROJECT / DECISIONS / STATE 三份活文档创建并放入仓库根目录

---

## 进行中

- [ ] 四个 gate 决策定稿（见 `PROJECT.md` §3）
- [ ] Python 环境 + 虚拟环境（.venv）
- [ ] Track B：启动券商开户 / miniQMT 权限 / 程序化报备流程

---

## 下一步（按顺序）

1. 填定 gate 决策（券商档位、执行层 Windows 部署、确认数据源与频率）
2. 建 `.venv`，安装 akshare / pandas，跑通第一个取数脚本（拉一只股票的日线）
3. 进入 **Phase 1：数据管道**（清洗 + 等比复权对齐 + 落盘）

---

## 未决问题 / 阻塞

- 执行层 Windows 环境尚未确定（本机 / VM / 云）
- 券商档位待定，取决于计划投入本金

---

## 备注

- GitHub 国内访问需代理；如不稳可考虑 Gitee 镜像备份。
- 本仓库根目录 = 项目的家，所有命令在带 `.git` 的那层执行。
