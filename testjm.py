import jmcomic

# 创建配置对象
option = jmcomic.create_option_by_file('D:\\astrfor\\option.yml')
# 使用option对象来下载本子
jmcomic.download_album(1422696, option)
# 等价写法: option.download_album(123)