<p align="center">
<a href="https://dscvit.com">
	<img width="400" src="https://user-images.githubusercontent.com/56252312/159312411-58410727-3933-4224-b43e-4e9b627838a3.png#gh-light-mode-only" alt="GDSC VIT"/>
</a>
	<h2 align="center"> < Insert Project Title Here > </h2>
	<h4 align="center"> < Insert Project Description Here > </h4>
</p>

---
[![Join Us](https://img.shields.io/badge/Join%20Us-Developer%20Student%20Clubs-red)](https://dsc.community.dev/vellore-institute-of-technology/)
[![Discord Chat](https://img.shields.io/discord/760928671698649098.svg)](https://discord.gg/498KVdSKWR)

[![DOCS](https://img.shields.io/badge/Documentation-see%20docs-green?style=flat-square&logo=appveyor)](INSERT_LINK_FOR_DOCS_HERE) 
[![UI ](https://img.shields.io/badge/User%20Interface-Link%20to%20UI-orange?style=flat-square&logo=appveyor)](INSERT_UI_LINK_HERE)

---

## Preview
Here are some images related to the project:

<p align="center">
	<img src="demand_forecast/assets/1.png" width="400" alt="Image 1"/>
	<img src="demand_forecast/assets/2.png" width="400" alt="Image 2"/>
	<img src="demand_forecast/assets/3.png" width="400" alt="Image 3"/>
	<img src="demand_forecast/assets/4.png" width="400" alt="Image 4"/>
	<img src="demand_forecast/assets/5.png" width="400" alt="Image 5"/>
	<img src="demand_forecast/assets/6.png" width="400" alt="Image 6"/>
	<img src="demand_forecast/assets/7.png" width="400" alt="Image 7"/>
	<img src="demand_forecast/assets/8.png" width="400" alt="Image 8"/>
	<img src="demand_forecast/assets/9.png" width="400" alt="Image 9"/>
	<img src="demand_forecast/assets/10.png" width="400" alt="Image 10"/>
</p>

---

## Features
- [x]  curl -X POST http://127.0.0.1:5000/extract -H "Content-Type: application/json" -d "{\"text\": \"John Doe bought 2 apples for $5\"}"

```json
{"CustomerName":"John Doe","ItemName":"apples","ItemQuantity":"2","Price":"5"}
```

- [x] curl -X POST "http://127.0.0.1:5000/extract_entities" -H "Content-Type: application/json" -d "{\"text\":\"apples less than 50 rs\"}"

```json
{"action":"less","object":"apples","range":"50"}
```

## Running

### Installation
```bash
docker-compose up --build 
docker-compose up 
```

### Execution
```bash
< insert code >
```

---

## Contributors

<table>
	<tr align="center">
		<td>
		Souvik Mahanta
		<p align="center">
			<img src="https://dscvit.com/images/dsc-logo-square.svg" width="150" height="150" alt="Souvik Mahanta">
		</p>
			<p align="center">
				<a href="https://github.com/souvik03-136">
					<img src="http://www.iconninja.com/files/241/825/211/round-collaboration-social-github-code-circle-network-icon.svg" width="36" height="36" alt="GitHub"/>
				</a>
				<a href="https://www.linkedin.com/in/souvik-mahanta/">
					<img src="http://www.iconninja.com/files/863/607/751/network-linkedin-social-connection-circular-circle-media-icon.svg" width="36" height="36" alt="LinkedIn"/>
				</a>
			</p>
		</td>
	</tr>
</table>

<p align="center">
	Made with ❤ by <a href="https://dscvit.com">GDSC-VIT</a>
</p>
