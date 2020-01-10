const Mustache = require('mustache'),
			fs = require('fs'),
			path = require('path');

function random(b, e) {
	return Math.round(Math.random() * (e - b) + b);
}

exports.handler = async function(event, context) {
	random_numbers = new Array(event.random_len)
  for(var i = 0; i < event.random_len; ++i) {
    random_numbers[i] = random(0, 100)
  }
	var input = {
		cur_time: new Date().toLocaleString(),
		username: event.username,
		random_numbers: random_numbers
	};

	var file = path.join('templates', 'template.html');
	return fs.readFile(file, "utf-8",
		function(err, data) {
			if(err) throw(err);	
			var output = Mustache.render(data, input);
			console.log(output)
			return output;
		}
	);	
}