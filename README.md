# 概要
新規 AWS Organizations メンバーアカウントが作成されたタイミングで、
該当アカウントの SSM Service Setting
ssm/documents/console/public-sharing-permission を Disable（パブリック共有ブロック） に自動設定する仕組みです。

# 構成図
<img width="1223" height="627" alt="image" src="https://github.com/user-attachments/assets/663953e5-43f6-4a80-87d1-5500f75f0d1d" />

